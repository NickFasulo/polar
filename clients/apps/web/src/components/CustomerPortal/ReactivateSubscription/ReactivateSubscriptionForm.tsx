'use client'

import { useCustomerPortalCustomer } from '@/hooks/queries/customerPortal'
import { setValidationErrors } from '@/utils/api/errors'
import { type Client, type schemas } from '@polar-sh/client'
import { formatCurrency } from '@polar-sh/currency'
import { Alert, Button } from '@polar-sh/orbit'
import { Form } from '@polar-sh/ui/components/ui/form'
import { useElements, useStripe } from '@stripe/react-stripe-js'
import { useEffect, useState } from 'react'
import { useForm, useFormState } from 'react-hook-form'
import {
  type CustomerPortalCustomerUpdate,
  getBillingDetailsValues,
  hasCompleteBillingAddress,
} from '../BillingDetailsFields'
import { BillingDetailsCard } from './BillingDetailsCard'
import { PaymentMethodCard } from './PaymentMethodCard'
import { useReactivationPayment } from './useReactivationPayment'

type SavedCard = schemas['PaymentMethodCard'] | schemas['PaymentMethodKrCard']

export const ReactivateSubscriptionForm = ({
  api,
  subscription,
  customer,
  preview,
  refetchPreview,
  savedCards,
  onSucceeded,
}: {
  api: Client
  subscription: schemas['CustomerSubscription']
  customer: schemas['CustomerPortalCustomer']
  preview: schemas['SubscriptionReactivationPreview'] | undefined
  refetchPreview: () => Promise<{
    data?: schemas['SubscriptionReactivationPreview']
  }>
  savedCards: SavedCard[]
  onSucceeded: () => void
}) => {
  const stripe = useStripe()
  const elements = useElements()
  const { update } = useCustomerPortalCustomer()
  const { state, pay, reset } = useReactivationPayment({
    api,
    subscriptionId: subscription.id,
  })
  const [savedCardId, setSavedCardId] = useState<string | null>(null)
  const [useNewCard, setUseNewCard] = useState(savedCards.length === 0)
  const [notice, setNotice] = useState<string | null>(null)
  const [editingBilling, setEditingBilling] = useState(
    () => !hasCompleteBillingAddress(customer.billing_address),
  )

  const form = useForm<CustomerPortalCustomerUpdate>({
    defaultValues: getBillingDetailsValues(customer),
  })
  const { control, handleSubmit, setError } = form
  const { isDirty } = useFormState({ control })

  useEffect(() => {
    if (state.status === 'succeeded') {
      onSucceeded()
    }
  }, [state.status, onSucceeded])

  const saveBillingDetails = async (data: CustomerPortalCustomerUpdate) => {
    try {
      const updatedCustomer = await update.mutateAsync(data)
      form.reset(getBillingDetailsValues(updatedCustomer))
      if (hasCompleteBillingAddress(updatedCustomer.billing_address)) {
        setEditingBilling(false)
      }
      return true
    } catch (e: unknown) {
      if (e != null && typeof e === 'object' && 'errors' in e) {
        setValidationErrors(
          (e as { errors: schemas['ValidationError'][] }).errors,
          setError,
        )
      }
      return false
    }
  }

  const onSubmit = async (data: CustomerPortalCustomerUpdate) => {
    if (!stripe || (useNewCard && !elements) || (!useNewCard && !savedCardId)) {
      return
    }
    setNotice(null)
    reset()

    if (isDirty || !preview) {
      if (!(await saveBillingDetails(data))) {
        return
      }
      const { data: refreshed } = await refetchPreview()
      if (!refreshed) {
        setNotice('Complete your billing address to continue.')
        return
      }
      if (refreshed.due_amount !== preview?.due_amount) {
        setNotice(
          `Your total is now ${formatCurrency('compact')(refreshed.due_amount, refreshed.currency)}, including tax for your billing address. Review it and confirm to pay.`,
        )
        return
      }
    }

    await pay(
      stripe,
      useNewCard && elements
        ? { elements }
        : { paymentMethodId: savedCardId as string },
    )
  }

  const isBusy =
    update.isPending ||
    state.status === 'paying' ||
    state.status === 'confirming' ||
    state.status === 'succeeded'

  return (
    <Form {...form}>
      <form onSubmit={handleSubmit(onSubmit)} className="flex flex-col gap-y-6">
        <BillingDetailsCard
          customer={customer}
          editing={editingBilling}
          onEdit={() => setEditingBilling(true)}
          onCancel={
            hasCompleteBillingAddress(customer.billing_address)
              ? () => {
                  form.reset(getBillingDetailsValues(customer))
                  setEditingBilling(false)
                }
              : undefined
          }
          disabled={isBusy}
        />
        <PaymentMethodCard
          savedCards={savedCards}
          useNewCard={useNewCard}
          onUseNewCardChange={setUseNewCard}
          onSelectSavedCard={setSavedCardId}
          disabled={isBusy}
        />
        {notice && <Alert variant="info" title={notice} />}
        {state.status === 'failed' && (
          <Alert
            variant="danger"
            title="Payment failed"
            description={state.error}
          />
        )}
        {state.status === 'confirming' && (
          <Alert
            variant="info"
            loading
            title="Confirming your payment…"
            description="This can take a few moments. Keep this page open."
          />
        )}
        <Button
          type="submit"
          size="lg"
          fullWidth
          loading={isBusy}
          disabled={isBusy || !stripe || (!useNewCard && !savedCardId)}
        >
          {preview
            ? `Pay ${formatCurrency('compact')(preview.due_amount, preview.currency)} and subscribe`
            : 'Continue'}
        </Button>
      </form>
    </Form>
  )
}
