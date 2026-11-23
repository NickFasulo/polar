import { useCustomerSubscriptionReactivate } from '@/hooks/queries/customerPortal'
import { extractApiErrorMessage } from '@/utils/api/errors'
import { type Client, type schemas } from '@polar-sh/client'
import type { Stripe, StripeElements } from '@stripe/stripe-js'
import { useEffect, useState } from 'react'

const POLL_INTERVAL_MS = 2000
const MAX_POLL_ATTEMPTS = 90

export type ReactivationPaymentSource =
  | { elements: StripeElements }
  | { paymentMethodId: string }

export type ReactivationPaymentState =
  | { status: 'idle' }
  | { status: 'paying' }
  | { status: 'confirming' }
  | { status: 'succeeded' }
  | { status: 'failed'; error: string }

const EMPTY_BILLING_DETAILS = {
  name: null,
  email: null,
  phone: null,
  address: {
    line1: null,
    line2: null,
    postal_code: null,
    city: null,
    state: null,
    country: null,
  },
}

const createConfirmationToken = async (
  stripe: Stripe,
  elements: StripeElements,
): Promise<{ id: string } | { error: string }> => {
  const { error: submitError } = await elements.submit()
  if (submitError) {
    return { error: submitError.message || 'Check your card details.' }
  }
  const { confirmationToken, error } = await stripe.createConfirmationToken({
    elements,
    params: {
      payment_method_data: { billing_details: EMPTY_BILLING_DETAILS },
    },
  })
  if (error || !confirmationToken) {
    return { error: error?.message || 'Check your card details.' }
  }
  return { id: confirmationToken.id }
}

export const useReactivationPayment = ({
  api,
  subscriptionId,
}: {
  api: Client
  subscriptionId: string
}) => {
  const reactivate = useCustomerSubscriptionReactivate(api)
  const [state, setState] = useState<ReactivationPaymentState>({
    status: 'idle',
  })

  useEffect(() => {
    if (state.status !== 'confirming') {
      return
    }
    let attempts = 0
    const interval = setInterval(async () => {
      attempts++
      const { data } = await api.GET('/v1/customer-portal/subscriptions/{id}', {
        params: { path: { id: subscriptionId } },
      })
      if (data?.status === 'active') {
        clearInterval(interval)
        setState({ status: 'succeeded' })
      } else if (attempts >= MAX_POLL_ATTEMPTS) {
        clearInterval(interval)
        setState({
          status: 'failed',
          error:
            'Your payment is still being confirmed. Check your subscription again in a few minutes.',
        })
      }
    }, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [state.status, api, subscriptionId])

  const fail = (error: string) => setState({ status: 'failed', error })

  const handleConfirmation = async (
    stripe: Stripe,
    result: schemas['CustomerOrderPaymentConfirmation'],
  ) => {
    if (result.status === 'succeeded' || result.status === 'processing') {
      setState({ status: 'confirming' })
      return
    }
    if (result.status === 'requires_action' && result.client_secret) {
      const { error } = await stripe.handleNextAction({
        clientSecret: result.client_secret,
      })
      if (error) {
        fail(error.message || 'Your card could not be authenticated.')
      } else {
        setState({ status: 'confirming' })
      }
      return
    }
    fail(result.error || 'Your payment failed. Try again or use another card.')
  }

  const pay = async (stripe: Stripe, source: ReactivationPaymentSource) => {
    setState({ status: 'paying' })

    let body: schemas['CustomerOrderConfirmPayment']
    if ('elements' in source) {
      const token = await createConfirmationToken(stripe, source.elements)
      if ('error' in token) {
        fail(token.error)
        return
      }
      body = { confirmation_token_id: token.id, payment_processor: 'stripe' }
    } else {
      body = {
        payment_method_id: source.paymentMethodId,
        payment_processor: 'stripe',
      }
    }

    try {
      const { data, error } = await reactivate.mutateAsync({
        id: subscriptionId,
        body,
      })
      if (error || !data) {
        fail(extractApiErrorMessage(error ?? {}, 'Your payment failed.'))
        return
      }
      await handleConfirmation(stripe, data)
    } catch {
      fail('Network error. Check your connection and try again.')
    }
  }

  return { state, pay, reset: () => setState({ status: 'idle' }) }
}
