import uuid
from collections.abc import Sequence
from datetime import timedelta
from enum import StrEnum
from typing import Any

import stripe as stripe_lib
from sqlalchemy import Select, UnaryExpression, asc, desc, select
from sqlalchemy.orm import contains_eager, joinedload, selectinload
from sqlalchemy.sql.elements import OrderByList

from polar.auth.models import AuthSubject, Customer, Member
from polar.enums import PaymentProcessor
from polar.exceptions import PolarError
from polar.integrations.stripe.service import STRIPE_METADATA_PAYMENT_TRIGGER
from polar.integrations.stripe.service import stripe as stripe_service
from polar.kit.db.postgres import AsyncSession
from polar.kit.pagination import PaginationParams, paginate
from polar.kit.services import ResourceServiceReader
from polar.kit.sorting import Sorting
from polar.kit.visibility import Visibility
from polar.models import (
    Organization,
    PaymentMethod,
    Product,
    Subscription,
    SubscriptionMeter,
)
from polar.models.payment import PaymentTrigger
from polar.models.subscription import CustomerCancellationReason, SubscriptionStatus
from polar.order.service import PaymentRetryValidationError
from polar.payment_method.repository import PaymentMethodRepository
from polar.payment_method.service import payment_method as payment_method_service
from polar.redis import Redis
from polar.subscription.repository import SubscriptionRepository
from polar.subscription.schemas import (
    SubscriptionChangePreview,
    SubscriptionReactivationPreview,
)
from polar.subscription.service import (
    AlreadyCanceledSubscription,
    SubscriptionUpdateContext,
)
from polar.subscription.service import subscription as subscription_service

from ..schemas.order import (
    CustomerOrderConfirmPayment,
    CustomerOrderPaymentConfirmation,
)
from ..schemas.subscription import (
    CustomerSubscriptionChange,
    CustomerSubscriptionChangeSeats,
    CustomerSubscriptionChangeUnits,
    CustomerSubscriptionPause,
    CustomerSubscriptionReactivate,
    CustomerSubscriptionResume,
    CustomerSubscriptionUpdate,
    CustomerSubscriptionUpdateClear,
    CustomerSubscriptionUpdateProduct,
    CustomerSubscriptionUpdateSeats,
    CustomerSubscriptionUpdateUnits,
)
from ..utils import get_customer_id
from .customer import customer as customer_service

TRIAL_REACTIVATION_LOCK_TTL = int(timedelta(days=1).total_seconds())


class CustomerSubscriptionError(PolarError): ...


class UpdateSubscriptionPlanNotAllowed(CustomerSubscriptionError):
    def __init__(self) -> None:
        super().__init__("Updating subscription plan is not allowed.", 403)


class UpdateSubscriptionSeatsNotAllowed(CustomerSubscriptionError):
    def __init__(self) -> None:
        super().__init__("Updating subscription seats is not allowed.", 403)


class UpdateSubscriptionUnitsNotAllowed(CustomerSubscriptionError):
    def __init__(self) -> None:
        super().__init__("Updating subscription units is not allowed.", 403)


class PauseResumeNotAllowed(CustomerSubscriptionError):
    def __init__(self) -> None:
        super().__init__("Pausing or resuming a subscription is not allowed.", 403)


class RevokeNotAllowed(CustomerSubscriptionError):
    def __init__(self) -> None:
        super().__init__(
            "This subscription can only be revoked while it is past-due "
            "with no benefit grace period.",
            409,
        )


class PaymentMethodRequired(CustomerSubscriptionError):
    def __init__(self, action: str) -> None:
        super().__init__(
            f"Add a payment method before {action} this subscription.",
            409,
        )


class NotReactivable(CustomerSubscriptionError):
    def __init__(self) -> None:
        super().__init__(
            "This subscription can only be reactivated after its trial ended.",
            409,
        )


class ReactivationInProgress(CustomerSubscriptionError):
    def __init__(self) -> None:
        super().__init__(
            "A payment to reactivate this subscription is already in progress.", 409
        )


class CustomerSubscriptionSortProperty(StrEnum):
    started_at = "started_at"
    amount = "amount"
    status = "status"
    organization = "organization"
    product = "product"


class CustomerSubscriptionService(ResourceServiceReader[Subscription]):
    async def list(
        self,
        session: AsyncSession,
        auth_subject: AuthSubject[Customer | Member],
        *,
        product_id: Sequence[uuid.UUID] | None = None,
        active: bool | None = None,
        query: str | None = None,
        pagination: PaginationParams,
        sorting: Sequence[Sorting[CustomerSubscriptionSortProperty]] = (
            (CustomerSubscriptionSortProperty.started_at, True),
        ),
    ) -> tuple[Sequence[Subscription], int]:
        statement = self._get_readable_subscription_statement(auth_subject)

        statement = (
            statement.join(Product, onclause=Subscription.product_id == Product.id)
            .join(Organization, onclause=Product.organization_id == Organization.id)
            .options(
                joinedload(Subscription.customer).joinedload(Customer.organization),
                contains_eager(Subscription.product).options(
                    selectinload(Product.product_medias),
                    contains_eager(Product.organization),
                ),
                selectinload(Subscription.meters).joinedload(SubscriptionMeter.meter),
                joinedload(Subscription.pending_update),
            )
        )

        if product_id is not None:
            statement = statement.where(Subscription.product_id.in_(product_id))

        if active is not None:
            if active:
                statement = statement.where(Subscription.active)
            else:
                statement = statement.where(Subscription.revoked)

        if query is not None:
            statement = statement.where(Product.name.icontains(query, autoescape=True))

        order_by_clauses: list[OrderByList | UnaryExpression[Any]] = []
        for criterion, is_desc in sorting:
            clause_function = desc if is_desc else asc
            if criterion == CustomerSubscriptionSortProperty.started_at:
                order_by_clauses.append(clause_function(Subscription.started_at))
            elif criterion == CustomerSubscriptionSortProperty.amount:
                order_by_clauses.append(clause_function(Subscription.amount))
            elif criterion == CustomerSubscriptionSortProperty.status:
                order_by_clauses.append(clause_function(Subscription.status))
            elif criterion == CustomerSubscriptionSortProperty.organization:
                order_by_clauses.append(clause_function(Organization.slug))
            elif criterion == CustomerSubscriptionSortProperty.product:
                order_by_clauses.append(clause_function(Product.name))
        statement = statement.order_by(*order_by_clauses)

        return await paginate(session, statement, pagination=pagination)

    async def get_by_id(
        self,
        session: AsyncSession,
        auth_subject: AuthSubject[Customer | Member],
        id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> Subscription | None:
        statement = (
            self._get_readable_subscription_statement(auth_subject)
            .where(Subscription.id == id)
            .options(
                joinedload(Subscription.customer),
                joinedload(Subscription.organization),
                joinedload(Subscription.product).options(
                    selectinload(Product.product_medias),
                    joinedload(Product.organization),
                ),
                selectinload(Subscription.meters).joinedload(SubscriptionMeter.meter),
                joinedload(Subscription.pending_update),
            )
        )

        if for_update:
            statement = statement.with_for_update(of=Subscription)

        result = await session.execute(statement)
        return result.scalar_one_or_none()

    async def update(
        self,
        session: AsyncSession,
        subscription: Subscription,
        *,
        updates: CustomerSubscriptionUpdate,
    ) -> Subscription:
        organization = subscription.product.organization
        if isinstance(updates, CustomerSubscriptionUpdateProduct):
            if not organization.customer_portal_subscription_update_plan:
                raise UpdateSubscriptionPlanNotAllowed()

            return await self.update_product(
                session,
                subscription,
                product_id=updates.product_id,
            )

        if isinstance(updates, CustomerSubscriptionUpdateSeats):
            if not organization.customer_portal_subscription_update_seats:
                raise UpdateSubscriptionSeatsNotAllowed()

            async with SubscriptionUpdateContext(
                session, subscription, subscription_service
            ) as ctx:
                return await subscription_service.update_seats(
                    session,
                    ctx,
                    subscription,
                    seats=updates.seats,
                )

        if isinstance(updates, CustomerSubscriptionUpdateUnits):
            if not organization.customer_portal_subscription_update_units:
                raise UpdateSubscriptionUnitsNotAllowed()

            async with SubscriptionUpdateContext(
                session, subscription, subscription_service
            ) as ctx:
                return await subscription_service.update_units(
                    session,
                    ctx,
                    subscription,
                    units=updates.units,
                )

        if isinstance(updates, CustomerSubscriptionUpdateClear):
            async with SubscriptionUpdateContext(
                session, subscription, subscription_service
            ) as ctx:
                return await subscription_service.clear_pending_update(
                    session, ctx, subscription
                )

        if isinstance(updates, CustomerSubscriptionPause):
            if not organization.customer_portal_subscription_pause:
                raise PauseResumeNotAllowed()

            async with SubscriptionUpdateContext(
                session, subscription, subscription_service
            ) as ctx:
                if updates.pause_at_period_end:
                    return await subscription_service.pause(
                        session, ctx, subscription, resumes_at=updates.resumes_at
                    )
                return await subscription_service.cancel_scheduled_pause(
                    session, ctx, subscription
                )

        if isinstance(updates, CustomerSubscriptionResume):
            if not organization.customer_portal_subscription_pause:
                raise PauseResumeNotAllowed()

            return await self.resume(session, subscription)

        if isinstance(updates, CustomerSubscriptionReactivate):
            async with SubscriptionUpdateContext(
                session, subscription, subscription_service
            ) as ctx:
                return await subscription_service.reactivate_ended_trial(
                    session, ctx, subscription
                )

        cancel = updates.cancel_at_period_end is True
        uncancel = updates.cancel_at_period_end is False
        if not (cancel or uncancel):
            return subscription

        if cancel:
            return await self.cancel(
                session,
                subscription,
                reason=updates.cancellation_reason,
                comment=updates.cancellation_comment,
            )

        return await self.uncancel(session, subscription)

    async def preview_change(
        self,
        session: AsyncSession,
        subscription: Subscription,
        *,
        change: CustomerSubscriptionChange,
    ) -> SubscriptionChangePreview:
        organization = subscription.product.organization
        if isinstance(change, CustomerSubscriptionChangeSeats):
            if not organization.customer_portal_subscription_update_seats:
                raise UpdateSubscriptionSeatsNotAllowed()
            return await subscription_service.calculate_change_preview(
                session,
                subscription,
                seats=change.seats,
            )

        if isinstance(change, CustomerSubscriptionChangeUnits):
            if not organization.customer_portal_subscription_update_units:
                raise UpdateSubscriptionUnitsNotAllowed()
            return await subscription_service.calculate_change_preview(
                session,
                subscription,
                units=change.units,
            )

        if not organization.customer_portal_subscription_update_plan:
            raise UpdateSubscriptionPlanNotAllowed()
        return await subscription_service.calculate_change_preview(
            session,
            subscription,
            product_id=change.product_id,
            allowed_visibilities=frozenset({Visibility.public}),
        )

    async def update_product(
        self,
        session: AsyncSession,
        subscription: Subscription,
        *,
        product_id: uuid.UUID,
    ) -> Subscription:
        async with SubscriptionUpdateContext(
            session, subscription, subscription_service
        ) as ctx:
            return await subscription_service.update_product(
                session,
                ctx,
                subscription,
                product_id=product_id,
                allowed_visibilities=frozenset({Visibility.public}),
            )

    async def resume(
        self,
        session: AsyncSession,
        subscription: Subscription,
    ) -> Subscription:
        if (
            subscription.can_resume()
            and subscription.organization.can_renew_subscriptions
        ):
            await self._require_payment_method(session, subscription, "resuming")

        async with SubscriptionUpdateContext(
            session, subscription, subscription_service
        ) as ctx:
            return await subscription_service.resume(session, ctx, subscription)

    async def uncancel(
        self,
        session: AsyncSession,
        subscription: Subscription,
    ) -> Subscription:
        if subscription.can_uncancel():
            await self._require_payment_method(session, subscription, "uncancelling")

        async with SubscriptionUpdateContext(
            session, subscription, subscription_service
        ) as ctx:
            return await subscription_service.uncancel(
                session,
                ctx,
                subscription,
            )

    async def cancel(
        self,
        session: AsyncSession,
        subscription: Subscription,
        *,
        reason: CustomerCancellationReason | None = None,
        comment: str | None = None,
    ) -> Subscription:
        async with SubscriptionUpdateContext(
            session, subscription, subscription_service, customer_initiated=True
        ) as ctx:
            return await subscription_service.cancel(
                session,
                ctx,
                subscription,
                customer_reason=reason,
                customer_comment=comment,
            )

    async def revoke(
        self,
        session: AsyncSession,
        subscription: Subscription,
        *,
        reason: CustomerCancellationReason | None = None,
        comment: str | None = None,
    ) -> Subscription:
        if not subscription.can_cancel(True):
            raise AlreadyCanceledSubscription(subscription)

        preview = await subscription_service.calculate_cancel_preview(
            session, subscription
        )
        if not preview.stops_collection:
            raise RevokeNotAllowed()

        async with SubscriptionUpdateContext(
            session, subscription, subscription_service, customer_initiated=True
        ) as ctx:
            return await subscription_service.revoke(
                session,
                ctx,
                subscription,
                customer_reason=reason,
                customer_comment=comment,
            )

    async def get_reactivation_preview(
        self, session: AsyncSession, subscription: Subscription
    ) -> SubscriptionReactivationPreview:
        if subscription.status != SubscriptionStatus.trial_ended:
            raise NotReactivable()

        subscription = await self._get_with_eager_options(session, subscription)
        return await subscription_service.preview_trial_reactivation(
            session, subscription
        )

    async def reactivate(
        self,
        session: AsyncSession,
        redis: Redis,
        subscription: Subscription,
        confirmation: CustomerOrderConfirmPayment,
    ) -> CustomerOrderPaymentConfirmation:
        """Pay to reactivate a trial_ended subscription, with the customer present.

        The payment comes first: the order is only created, and the subscription
        activated, by the webhook once the payment succeeds.
        """
        if subscription.status != SubscriptionStatus.trial_ended:
            raise NotReactivable()

        return await self._start_trial_reactivation_payment(
            session, redis, subscription, confirmation
        )

    async def _start_trial_reactivation_payment(
        self,
        session: AsyncSession,
        redis: Redis,
        subscription: Subscription,
        confirmation: CustomerOrderConfirmPayment,
    ) -> CustomerOrderPaymentConfirmation:
        if confirmation.payment_processor != PaymentProcessor.stripe:
            raise NotReactivable()

        lock_key = f"subscription:{subscription.id}:trial_reactivation_intent"
        previous_intent_id = await redis.get(lock_key)
        if previous_intent_id is not None:
            previous_intent = await stripe_service.get_payment_intent(
                previous_intent_id
            )
            if previous_intent.status in {"processing", "succeeded"}:
                raise ReactivationInProgress()
            if previous_intent.status == "requires_action":
                await stripe_service.cancel_payment_intent(previous_intent.id)

        subscription = await self._get_with_eager_options(session, subscription)
        customer = subscription.customer

        saved_payment_method: PaymentMethod | None = None
        if confirmation.payment_method_id is not None:
            saved_payment_method = await PaymentMethodRepository.from_session(
                session
            ).get_by_id(confirmation.payment_method_id)
            if (
                saved_payment_method is None
                or saved_payment_method.customer_id != customer.id
            ):
                raise PaymentRetryValidationError(
                    "Payment method does not belong to customer"
                )

        stripe_customer_id = await customer_service.get_or_create_stripe_customer_id(
            session, customer
        )
        subscription_id = subscription.id
        metadata: dict[str, str] = {
            "organization_id": str(subscription.organization_id),
            "subscription_id": str(subscription_id),
            STRIPE_METADATA_PAYMENT_TRIGGER: PaymentTrigger.trial_reactivation,
        }
        description = f"{subscription.organization.name} — {subscription.product.name}"

        preview = await subscription_service.preview_trial_reactivation(
            session, subscription
        )

        payment_method_params: dict[str, Any]
        if saved_payment_method is not None:
            payment_method_params = {
                "payment_method": saved_payment_method.processor_id,
                "automatic_payment_methods": {
                    "enabled": True,
                    "allow_redirects": "never",
                },
            }
        else:
            payment_method_params = {
                "confirmation_token": confirmation.confirmation_token_id,
                "setup_future_usage": "off_session",
                "automatic_payment_methods": {
                    "enabled": True,
                    "allow_redirects": "never",
                },
            }

        try:
            payment_intent = await stripe_service.create_payment_intent(
                amount=preview.due_amount,
                currency=preview.currency,
                customer=stripe_customer_id,
                confirm=True,
                description=description,
                metadata=metadata,
                **payment_method_params,
            )
        except stripe_lib.StripeError as e:
            return CustomerOrderPaymentConfirmation(
                status="failed",
                client_secret=None,
                error=(
                    e.error.message
                    if e.error and e.error.message
                    else "Payment failed. Please try again."
                ),
            )

        await redis.set(lock_key, payment_intent.id, ex=TRIAL_REACTIVATION_LOCK_TTL)

        if payment_intent.status in {"succeeded", "processing"}:
            return CustomerOrderPaymentConfirmation(
                status=payment_intent.status, client_secret=None, error=None
            )
        if payment_intent.status == "requires_action":
            return CustomerOrderPaymentConfirmation(
                status="requires_action",
                client_secret=payment_intent.client_secret,
                error=None,
            )
        return CustomerOrderPaymentConfirmation(
            status="failed",
            client_secret=None,
            error=(
                payment_intent.last_payment_error.message
                if payment_intent.last_payment_error
                and payment_intent.last_payment_error.message
                else "Payment failed"
            ),
        )

    async def _get_with_eager_options(
        self, session: AsyncSession, subscription: Subscription
    ) -> Subscription:
        repository = SubscriptionRepository.from_session(session)
        loaded = await repository.get_by_id(
            subscription.id, options=repository.get_eager_options()
        )
        assert loaded is not None
        return loaded

    async def _require_payment_method(
        self, session: AsyncSession, subscription: Subscription, action: str
    ) -> None:
        if all(price.is_free for price in subscription.prices):
            return

        payment_method = await payment_method_service.get_customer_payment_method(
            session, subscription.customer
        )
        if payment_method is None:
            raise PaymentMethodRequired(action)

    def _get_readable_subscription_statement(
        self, auth_subject: AuthSubject[Customer | Member]
    ) -> Select[Subscription]:
        return select(Subscription).where(
            ~Subscription.is_deleted,
            Subscription.customer_id == get_customer_id(auth_subject),
        )


customer_subscription = CustomerSubscriptionService(Subscription)
