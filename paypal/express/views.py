# -*- coding: utf-8 -*-
from __future__ import unicode_literals
from decimal import Decimal as D
import logging

from django.views.generic import RedirectView, View
from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.contrib import messages
from django.contrib.auth.models import AnonymousUser
from django.urls import reverse
from django.http import HttpResponseRedirect
from django.utils.http import urlencode
from django.utils import six
from django.utils.translation import ugettext_lazy as _
from django.core.exceptions import ObjectDoesNotExist 

import oscar
from oscar.apps.payment.exceptions import UnableToTakePayment
from oscar.core.exceptions import ModuleNotFoundError
from oscar.core.loading import get_class, get_model
from oscar.apps.shipping.methods import FixedPrice, NoShippingRequired

from paypal.express.facade import (
    get_paypal_url, fetch_transaction_details, confirm_transaction)
from paypal.express.exceptions import (
    EmptyBasketException, MissingShippingAddressException,
    MissingShippingMethodException, InvalidBasket)
from paypal.exceptions import PayPalError

# Load views dynamically
try:
    from checkout.views import PaymentDetailsView
except:
    PaymentDetailsView = get_class('checkout.views', 'PaymentDetailsView')
CheckoutSessionMixin = get_class('checkout.session', 'CheckoutSessionMixin')
UnableToPlaceOrder = get_class('order.exceptions', 'UnableToPlaceOrder')
OrderNumberGenerator = get_class('order.utils', 'OrderNumberGenerator')
OrderCreator = get_class('order.utils', 'OrderCreator') 

ShippingAddress = get_model('order', 'ShippingAddress')
BillingAddress = get_model('order', 'BillingAddress') 
UserAddress = get_model('address', 'UserAddress')
Country = get_model('address', 'Country')
Basket = get_model('basket', 'Basket')
Repository = get_class('shipping.repository', 'Repository')
Selector = get_class('partner.strategy', 'Selector')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')
try:
    Applicator = get_class('offer.applicator', 'Applicator')
except ModuleNotFoundError:
    # fallback for django-oscar<=1.1
    Applicator = get_class('offer.utils', 'Applicator')

logger = logging.getLogger('paypal.express')


def custom_update_stock_records(self, line):
    # not allocate the produt if order pre-payment
    if getattr(self, 'payment_view', ''):
        if line.product.get_product_class().track_stock:
            line.stockrecord.allocate(line.quantity)

OrderCreator.update_stock_records = custom_update_stock_records


class CustomOrderFunction(object):

    def create_shipping_address(self, user, shipping_address):
        """
        Create and return the shipping address for the current order.
        Compared to self.get_shipping_address(), ShippingAddress is saved and
        makes sure that appropriate UserAddress exists.
        """
        # For an order that only contains items that don't require shipping we
        # won't have a shipping address, so we have to check for it.
        if not shipping_address:
            return None
        shipping_address.save()
        if user.is_authenticated():
            self.update_address_book(user, shipping_address)

        return shipping_address

    def create_billing_address(self, user, billing_address=None,
                               shipping_address=None, **kwargs):
        """
        Saves any relevant billing data (eg a billing address).
        """
        if not billing_address:
            return None
        billing_address.save()
        if user.is_authenticated():
            self.update_address_book(user, billing_address)
        return billing_address

    def submit(self, user, basket, shipping_address, shipping_method,  # noqa (too complex (10))
                   shipping_charge, billing_address, order_total,
                   payment_kwargs=None, order_kwargs=None):
            """
            Submit a basket for order placement.
            The process runs as follows:
             * Generate an order number
             * Freeze the basket so it cannot be modified any more (important when
               redirecting the user to another site for payment as it prevents the
               basket being manipulated during the payment process).
             * Attempt to take payment for the order
               - If payment is successful, place the order
               - If a redirect is required (eg PayPal, 3DSecure), redirect
               - If payment is unsuccessful, show an appropriate error message
            :basket: The basket to submit.
            :payment_kwargs: Additional kwargs to pass to the handle_payment
                             method. It normally makes sense to pass form
                             instances (rather than model instances) so that the
                             forms can be re-rendered correctly if payment fails.
            :order_kwargs: Additional kwargs to pass to the place_order method
            """
            if payment_kwargs is None:
                payment_kwargs = {}
            if order_kwargs is None:
                order_kwargs = {}
            # Taxes must be known at this point
            assert basket.is_tax_known, (
                "Basket tax must be set before a user can place an order")
            assert shipping_charge.is_tax_known, (
                "Shipping charge tax must be set before a user can place an order")

            # Se esiste già un ordine lo prendo e lo cancello per ricreare i nuovi dati
            basket_order = basket.order_set.first()
            print basket_order
            if basket_order:
                order_number = basket_order.number
                basket_order.delete()
            else:
                order_number = OrderNumberGenerator().order_number(basket)
            print order_number
            self.checkout_session.set_order_number(order_number)
            logger.info("Order #%s: beginning submission process for basket #%d",
                        order_number, basket.id)
            # Freeze the basket so it cannot be manipulated while the customer is
            # completing payment on a 3rd party site.  Also, store a reference to
            # the basket in the session so that we know which basket to thaw if we
            # get an unsuccessful payment response when redirecting to a 3rd party
            # site.
            if self.payment_view:
                basket.freeze()
            self.checkout_session.set_submitted_basket(basket)
            # We define a general error message for when an unanticipated payment
            # error occurs.
            error_msg = _("A problem occurred while processing payment for this "
                          "order - no payment has been taken.  Please "
                          "contact customer services if this problem persists")
            #signals.pre_payment.send_robust(sender=self, view=self)

            try:
              self.handle_payment(order_number, order_total, **payment_kwargs)
            except Exception as e:
                print e
            # If all is ok with payment, try and place order
            logger.info("Order #%s: payment successful, placing order",
                        order_number)
            try:
                return self.handle_order_placement(
                    order_number, user, basket, shipping_address, shipping_method,
                    shipping_charge, billing_address, order_total, **order_kwargs)
            except UnableToPlaceOrder as e:
                # It's possible that something will go wrong while trying to
                # actually place an order.  Not a good situation to be in as a
                # payment transaction may already have taken place, but needs
                # to be handled gracefully.
                print e
                msg = six.text_type(e)
                logger.error("Order #%s: unable to place order - %s",
                             order_number, msg, exc_info=True)
                self.restore_frozen_basket()
                #return self.render_preview(
                #    self.request, error=msg, **payment_kwargs)


    def handle_order_placement(self, order_number, user, basket,
                               shipping_address, shipping_method,
                               shipping_charge, billing_address, order_total,
                               **kwargs):
        """
        Write out the order models and return the appropriate HTTP response
        We deliberately pass the basket in here as the one tied to the request
        isn't necessarily the correct one to use in placing the order.  This
        can happen when a basket gets frozen.
        """
        order = self.place_order(
            order_number=order_number, user=user, basket=basket,
            shipping_address=shipping_address, shipping_method=shipping_method,
            shipping_charge=shipping_charge, order_total=order_total,
            billing_address=billing_address, **kwargs)
        #basket.submit()
        #self.checkout_session.flush()
        self.request.session['checkout_order_id'] = order.id
        if self.payment_view:
            return self.handle_successful_order(order)

    def place_order(self, order_number, user, basket, shipping_address,
                    shipping_method, shipping_charge, order_total,
                    billing_address=None, **kwargs):
        """
        Writes the order out to the DB including the payment models
        """
        # Create saved shipping address instance from passed in unsaved
        # instance
        shipping_address = self.create_shipping_address(user, shipping_address)
        # We pass the kwargs as they often include the billing address form
        # which will be needed to save a billing address.
        billing_address = self.create_billing_address(
            user, billing_address, shipping_address, **kwargs)
        if 'status' not in kwargs:
            status = None #self.get_initial_order_status(basket)
        else:
            status = kwargs.pop('status')
        print kwargs
        if 'request' not in kwargs:
            request = getattr(self, 'request', None)
        else:
            request = kwargs.pop('request')
        print kwargs
        order = OrderCreator().place_order(
            user=user,
            order_number=order_number,
            basket=basket,
            shipping_address=shipping_address,
            shipping_method=shipping_method,
            shipping_charge=shipping_charge,
            total=order_total,
            billing_address=billing_address,
            status=status,
            #request=request,
            **kwargs)
        self.save_payment_sources(order)
        return order

    def save_payment_sources(self, order):
        """
        Saves any payment sources used in this order.
        When the payment sources are created, the order model does not exist
        and so they need to have it set before saving.
        """
        if not self._payment_sources:
            return
        for source in self._payment_sources:
            source.order = order
            source.save()

    def update_address_book(self, user, addr):
            """
            Update the user's address book based on the new shipping address
            """
            try:
                user_addr = user.addresses.get(
                    hash=addr.generate_hash())
            except ObjectDoesNotExist:
                # Create a new user address
                user_addr = UserAddress(user=user)
                addr.populate_alternative_model(user_addr)
            try:
                if isinstance(addr, ShippingAddress):
                    user_addr.num_orders_as_shipping_address += 1
                if isinstance(addr, BillingAddress):
                    user_addr.num_orders_as_billing_address += 1
            except Exception, e:
                print e
            user_addr.save()


class RedirectView(CheckoutSessionMixin, RedirectView, CustomOrderFunction):
    """
    Initiate the transaction with Paypal and redirect the user
    to PayPal's Express Checkout to perform the transaction.
    """
    permanent = False

    # Setting to distinguish if the site has already collected a shipping
    # address.  This is False when redirecting to PayPal straight from the
    # basket page but True when redirecting from checkout.
    as_payment_method = True

    _payment_sources = None

    payment_view = False

    def get_redirect_url(self, **kwargs):
        try:
            basket = self.build_submission()['basket']
            url = self._get_redirect_url(basket, **kwargs)
        except PayPalError as ppe:
            messages.error(
                self.request, ppe.message)
            if self.as_payment_method:
                url = reverse('checkout:payment-details')
            else:
                url = reverse('basket:summary')
            return url
        except InvalidBasket as e:
            messages.warning(self.request, six.text_type(e))
            return reverse('basket:summary')
        except EmptyBasketException:
            messages.error(self.request, _("Your basket is empty"))
            return reverse('basket:summary')
        except MissingShippingAddressException:
            messages.error(
                self.request, _("A shipping address must be specified"))
            return reverse('checkout:shipping-address')
        except MissingShippingMethodException:
            messages.error(
                self.request, _("A shipping method must be specified"))
            return reverse('checkout:shipping-method')
        else:
            # Transaction successfully registered with PayPal.  Now freeze the
            # basket so it can't be edited while the customer is on the PayPal
            # site.
            #basket.freeze()

            logger.info("Basket #%s - redirecting to %s", basket.id, url)

            if getattr(settings, 'SAVE_ORDER_BEFORE_PAYPAL', False):
                #CREAZIONE ORDINE PRIMA DEL PAGAMENTO
                self.submit(**self.build_submission())

            return url

    def _get_redirect_url(self, basket, **kwargs):
        if basket.is_empty:
            raise EmptyBasketException()

        params = {
            'basket': basket,
            'shipping_methods': []          # setup a default empty list
        }                                   # to support no_shipping

        user = self.request.user
        if self.as_payment_method:
            if basket.is_shipping_required():
                # Only check for shipping details if required.
                shipping_addr = self.get_shipping_address(basket)
                if not shipping_addr:
                    raise MissingShippingAddressException()

                shipping_method = self.get_shipping_method(
                    basket, shipping_addr)
                if not shipping_method:
                    raise MissingShippingMethodException()

                params['shipping_address'] = shipping_addr
                params['shipping_method'] = shipping_method
                params['shipping_methods'] = []

        else:
            # Maik doubts that this code ever worked. Assigning
            # shipping method instances to Paypal params
            # isn't going to work, is it?
            shipping_methods = Repository().get_shipping_methods(
                user=user, basket=basket, request=self.request)
            params['shipping_methods'] = shipping_methods

        if settings.DEBUG:
            # Determine the localserver's hostname to use when
            # in testing mode
            params['host'] = self.request.META['HTTP_HOST']

        if user.is_authenticated():
            params['user'] = user

        params['paypal_params'] = self._get_paypal_params()

        return get_paypal_url(**params)

    def _get_paypal_params(self):
        """
        Return any additional PayPal parameters
        """
        return {}



class CancelResponseView(RedirectView):
    permanent = False

    def get(self, request, *args, **kwargs):
        basket = get_object_or_404(Basket, id=kwargs['basket_id'],
                                   status=Basket.FROZEN)
        basket.thaw()
        logger.info("Payment cancelled (token %s) - basket #%s thawed",
                    request.GET.get('token', '<no token>'), basket.id)
        return super(CancelResponseView, self).get(request, *args, **kwargs)

    def get_redirect_url(self, **kwargs):
        messages.error(self.request, _("PayPal transaction cancelled"))
        return reverse('basket:summary')


# Upgrading notes: when we drop support for Oscar 0.6, this class can be
# refactored to pass variables around more explicitly (instead of assigning
# things to self so they are accessible in a later method).
class SuccessResponseView(CustomOrderFunction, PaymentDetailsView):
    template_name_preview = 'paypal/express/preview.html'
    preview = True

    payment_view = True

    @property
    def pre_conditions(self):
        return []

    def get(self, request, *args, **kwargs):
        """
        Fetch details about the successful transaction from PayPal.  We use
        these details to show a preview of the order with a 'submit' button to
        place it.
        """
        print 'paypal success get'
        try:
            self.payer_id = request.GET['PayerID']
            self.token = request.GET['token']
        except KeyError:
            # Manipulation - redirect to basket page with warning message
            logger.warning("Missing GET params on success response page")
            messages.error(
                self.request,
                _("Unable to determine PayPal transaction details"))
            return HttpResponseRedirect(reverse('basket:summary'))

        try:
            self.txn = fetch_transaction_details(self.token)
        except PayPalError as e:
            logger.warning(
                "Unable to fetch transaction details for token %s: %s",
                self.token, e)
            messages.error(
                self.request,
                _("A problem occurred communicating with PayPal - please try again later"))
            return HttpResponseRedirect(reverse('basket:summary'))

        # Reload frozen basket which is specified in the URL
        # kwargs['basket'] = self.load_frozen_basket(kwargs['basket_id'])
        # if not kwargs['basket']:
        #     kwargs['basket'] = request.basket
        #     if not kwargs['basket']:
        #         logger.warning(
        #             "Unable to load frozen basket with ID %s", kwargs['basket_id'])
        #         messages.error(
        #             self.request,
        #             _("No basket was found that corresponds to your "
        #               "PayPal transaction"))
        #         return HttpResponseRedirect(reverse('basket:summary'))

        # logger.info(
        #     "Basket #%s - showing preview with payer ID %s and token %s",
        #     kwargs['basket'].id, self.payer_id, self.token)

        # basket = self.load_frozen_basket(kwargs['basket_id'])
        # if not basket:
        #     basket = request.basket
        basket = Basket.objects.get(id=kwargs['basket_id'])
        if Selector:
            basket.strategy = Selector().strategy(request)

        # Re-apply any offers
        Applicator().apply(basket, request.user, request=request)

        if not basket:
            messages.error(self.request, _("No basket was found that corresponds to your "
                                        "PayPal transaction"))
            return HttpResponseRedirect(reverse('basket:summary'))

        submission = self.build_submission(basket=basket)
        self.submit(**submission)
        super(SuccessResponseView, self).get(request, *args, **kwargs)
        return HttpResponseRedirect(reverse('checkout:thank-you'))

    def load_frozen_basket(self, basket_id):
        # Lookup the frozen basket that this txn corresponds to
        try:
            basket = Basket.objects.get(id=basket_id, status=Basket.FROZEN)
        except Basket.DoesNotExist:
            return None

        # Assign strategy to basket instance
        if Selector:
            basket.strategy = Selector().strategy(self.request)

        # Re-apply any offers
        Applicator().apply(basket, self.request.user, request=self.request)

        return basket

    def get_context_data(self, **kwargs):
        ctx = super(SuccessResponseView, self).get_context_data(**kwargs)

        if not hasattr(self, 'payer_id'):
            return ctx

        # This context generation only runs when in preview mode
        ctx.update({
            'payer_id': self.payer_id,
            'token': self.token,
            'paypal_user_email': self.txn.value('EMAIL'),
            'paypal_amount': D(self.txn.value('AMT')),
        })

        return ctx

    def post(self, request, *args, **kwargs):
        """
        Place an order.

        We fetch the txn details again and then proceed with oscar's standard
        payment details view for placing the order.
        """
        print 'paypal success post'
        error_msg = _(
            "A problem occurred communicating with PayPal "
            "- please try again later"
        )
        try:
            self.payer_id = request.POST['payer_id']
            self.token = request.POST['token']
        except KeyError:
            # Probably suspicious manipulation if we get here
            messages.error(self.request, error_msg)
            return HttpResponseRedirect(reverse('basket:summary'))

        try:
            self.txn = fetch_transaction_details(self.token)
        except PayPalError:
            # Unable to fetch txn details from PayPal - we have to bail out
            messages.error(self.request, error_msg)
            return HttpResponseRedirect(reverse('basket:summary'))

        # Reload frozen basket which is specified in the URL
        basket = self.load_frozen_basket(kwargs['basket_id'])
        if not basket:
            messages.error(self.request, error_msg)
            return HttpResponseRedirect(reverse('basket:summary'))

        submission = self.build_submission(basket=basket)
        return self.submit(**submission)

    def build_submission(self, **kwargs):
        submission = super(
            SuccessResponseView, self).build_submission(**kwargs)
        # Pass the user email so it can be stored with the order
                
        #submission['order_kwargs']['guest_email'] = submission['user'].email#self.txn.value('EMAIL')
        # Pass PP params
        submission['payment_kwargs']['payer_id'] = self.payer_id
        submission['payment_kwargs']['token'] = self.token
        submission['payment_kwargs']['txn'] = self.txn
        return submission

    def handle_payment(self, order_number, total, **kwargs):
        """
        Complete payment with PayPal - this calls the 'DoExpressCheckout'
        method to capture the money from the initial transaction.
        """
        try:
            confirm_txn = confirm_transaction(
                kwargs['payer_id'], kwargs['token'], kwargs['txn'].amount,
                kwargs['txn'].currency)
        except PayPalError:
            raise UnableToTakePayment()
        if not confirm_txn.is_successful:
            raise UnableToTakePayment()

        # Record payment source and event
        source_type, is_created = SourceType.objects.get_or_create(
            name='PayPal')
        source = Source(source_type=source_type,
                        currency=confirm_txn.currency,
                        amount_allocated=confirm_txn.amount,
                        amount_debited=confirm_txn.amount)
        self.add_payment_source(source)
        self.add_payment_event('Settled', confirm_txn.amount,
                               reference=confirm_txn.correlation_id)

    def get_shipping_address(self, basket):
        """
        Return a created shipping address instance, created using
        the data returned by PayPal.
        """
        # Determine names - PayPal uses a single field
        ship_to_name = self.txn.value('PAYMENTREQUEST_0_SHIPTONAME')
        if ship_to_name is None:
            return None
        first_name = last_name = None
        parts = ship_to_name.split()
        if len(parts) == 1:
            last_name = ship_to_name
        elif len(parts) > 1:
            first_name = parts[0]
            last_name = " ".join(parts[1:])

        #MODIFY modifica indirizzo
        addr_data = self.checkout_session.new_shipping_address_fields()
        if addr_data:
            # Load address data into a blank shipping address model
            return ShippingAddress(**addr_data)
        addr_id = self.checkout_session.shipping_user_address_id()
        if addr_id:
            try:
                address = UserAddress._default_manager.get(pk=addr_id)
            except UserAddress.DoesNotExist:
                # An address was selected but now it has disappeared.  This can
                # happen if the customer flushes their address book midway
                # through checkout.  No idea why they would do this but it can
                # happen.  Checkouts are highly vulnerable to race conditions
                # like this.
                return None
            else:
                # Copy user address data into a blank shipping address instance
                shipping_addr = ShippingAddress()
                address.populate_alternative_model(shipping_addr)
            return shipping_addr
        return None



        return ShippingAddress(
            first_name=first_name,
            last_name=last_name,
            line1=self.txn.value('PAYMENTREQUEST_0_SHIPTOSTREET'),
            line2=self.txn.value('PAYMENTREQUEST_0_SHIPTOSTREET2', default=""),
            line4=self.txn.value('PAYMENTREQUEST_0_SHIPTOCITY', default=""),
            state=self.txn.value('PAYMENTREQUEST_0_SHIPTOSTATE', default=""),
            postcode=self.txn.value('PAYMENTREQUEST_0_SHIPTOZIP', default=""),
            country=Country.objects.get(iso_3166_1_a2=self.txn.value('PAYMENTREQUEST_0_SHIPTOCOUNTRYCODE'))
        )

    def _get_shipping_method_by_name(self, name, basket, shipping_address=None):
        methods = Repository().get_shipping_methods(
            basket=basket, user=self.request.user,
            shipping_addr=shipping_address, request=self.request)
        for method in methods:
            if method.name == name:
                return method

    def get_shipping_method(self, basket, shipping_address=None, **kwargs):
        """
        Return the shipping method used
        """
        if not basket.is_shipping_required():
            return NoShippingRequired()

        # Instantiate a new FixedPrice shipping method instance
        charge_incl_tax = D(self.txn.value('PAYMENTREQUEST_0_SHIPPINGAMT'))

        # Assume no tax for now
        charge_excl_tax = charge_incl_tax
        name = self.txn.value('SHIPPINGOPTIONNAME')

        session_method = super(SuccessResponseView, self).get_shipping_method(
            basket, shipping_address, **kwargs)
        if not session_method or (name and name != session_method.name):
            if name:
                method = self._get_shipping_method_by_name(name, basket, shipping_address)
            else:
                method = None
            if not method:
                method = FixedPrice(charge_excl_tax, charge_incl_tax)
                if session_method:
                    method.name = session_method.name
                    method.code = session_method.code
        else:
            method = session_method
        return method


class ShippingOptionsView(View):

    def post(self, request, *args, **kwargs):
        """
        We use the shipping address given to use by PayPal to
        determine the available shipping method
        """
        # Basket ID is passed within the URL path.  We need to do this as some
        # shipping options depend on the user and basket contents.  PayPal do
        # pass back details of the basket contents but it would be royal pain to
        # reconstitute the basket based on those - easier to just to piggy-back
        # the basket ID in the callback URL.
        basket = get_object_or_404(Basket, id=kwargs['basket_id'])
        user = basket.owner
        if not user:
            user = AnonymousUser()

        # Create a shipping address instance using the data passed back
        country_code = self.request.POST.get(
            'PAYMENTREQUEST_0_SHIPTOCOUNTRY', None)
        try:
            country = Country.objects.get(iso_3166_1_a2=country_code)
        except Country.DoesNotExist:
            country = Country()

        shipping_address = ShippingAddress(
            line1=self.request.POST.get('PAYMENTREQUEST_0_SHIPTOSTREET', ''),
            line2=self.request.POST.get('PAYMENTREQUEST_0_SHIPTOSTREET2', ''),
            line4=self.request.POST.get('PAYMENTREQUEST_0_SHIPTOCITY', ''),
            state=self.request.POST.get('PAYMENTREQUEST_0_SHIPTOSTATE', ''),
            postcode=self.request.POST.get('PAYMENTREQUEST_0_SHIPTOZIP', ''),
            country=country
        )
        methods = Repository().get_shipping_methods(
            basket=basket, shipping_addr=shipping_address,
            request=self.request, user=user)
        return self.render_to_response(methods, basket)

    def render_to_response(self, methods, basket):
        pairs = [
            ('METHOD', 'CallbackResponse'),
            ('CURRENCYCODE', self.request.POST.get('CURRENCYCODE', 'GBP')),
        ]
        for index, method in enumerate(methods):
            charge = method.calculate(basket).incl_tax

            pairs.append(('L_SHIPPINGOPTIONNAME%d' % index,
                          six.text_type(method.name)))
            pairs.append(('L_SHIPPINGOPTIONLABEL%d' % index,
                          six.text_type(method.name)))
            pairs.append(('L_SHIPPINGOPTIONAMOUNT%d' % index, charge))
            # For now, we assume tax and insurance to be zero
            pairs.append(('L_TAXAMT%d' % index, D('0.00')))
            pairs.append(('L_INSURANCEAMT%d' % index, D('0.00')))
            # We assume that the first returned method is the default one
            pairs.append(('L_SHIPPINGOPTIONISDEFAULT%d' % index, 1 if index == 0 else 0))
        else:
            # No shipping methods available - we flag this up to PayPal indicating that we
            # do not ship to the shipping address.
            pairs.append(('NO_SHIPPING_OPTION_DETAILS', 1))
        payload = urlencode(pairs)
        return HttpResponse(payload)
