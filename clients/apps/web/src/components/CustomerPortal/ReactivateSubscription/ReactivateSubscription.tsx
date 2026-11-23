'use client'

import revalidate from '@/app/actions'
import { toast } from '@/components/Toast/use-toast'
import {
  invalidateReactivatedSubscription,
  useCustomerPaymentMethods,
  useCustomerPortalCustomer,
  useCustomerSubscriptionReactivationPreview,
} from '@/hooks/queries/customerPortal'
import { loadPolarStripe } from '@/utils/stripe'
import { type Client, type schemas } from '@polar-sh/client'
import { Button } from '@polar-sh/orbit'
import { Box } from '@polar-sh/orbit/Box'
import { getThemePreset } from '@polar-sh/ui/hooks/theming'
import { Elements } from '@stripe/react-stripe-js'
import { useTheme } from 'next-themes'
import Link from 'next/link'
import { useRouter, useSearchParams } from 'next/navigation'
import { useState } from 'react'
import { PortalEmptyState } from '../PortalPrimitives'
import { ReactivateSubscriptionForm } from './ReactivateSubscriptionForm'
import { ReactivationSummary } from './ReactivationSummary'

export const ReactivateSubscription = ({
  api,
  subscription,
}: {
  api: Client
  subscription: schemas['CustomerSubscription']
}) => {
  const router = useRouter()
  const searchParams = useSearchParams()
  const theme = useTheme()
  const themingPreset = getThemePreset(theme.resolvedTheme as 'light' | 'dark')
  const [stripePromise] = useState(() => loadPolarStripe())

  const subscriptionHref = `/${subscription.product.organization.slug}/portal/subscriptions/${subscription.id}?${searchParams.toString()}`
  const isReactivable = subscription.status === 'trial_ended'

  const { data: customer } = useCustomerPortalCustomer()
  const { data: paymentMethods } = useCustomerPaymentMethods(api)
  const { data: preview, refetch: refetchPreview } =
    useCustomerSubscriptionReactivationPreview(
      api,
      subscription.id,
      isReactivable,
    )

  const handleSucceeded = async () => {
    invalidateReactivatedSubscription()
    await revalidate('customer_portal')
    toast({
      title: 'Subscription reactivated',
      description: 'Your subscription is active again.',
    })
    router.push(subscriptionHref)
  }

  if (!isReactivable) {
    return (
      <PortalEmptyState
        title="Nothing to reactivate"
        description="This subscription doesn't need to be reactivated."
        actions={
          <Link href={subscriptionHref}>
            <Button variant="secondary">Back to subscription</Button>
          </Link>
        }
      />
    )
  }

  const savedCards = (paymentMethods?.items ?? []).filter(
    (pm): pm is schemas['PaymentMethodCard'] | schemas['PaymentMethodKrCard'] =>
      pm.type === 'card' ||
      (pm.type === 'kr_card' && subscription.currency === 'krw'),
  )

  return (
    <Box flexDirection="column" rowGap="xl">
      <ReactivationSummary subscription={subscription} preview={preview} />
      {customer && paymentMethods && (
        <Elements
          stripe={stripePromise}
          options={{
            locale: 'en',
            mode: 'payment',
            amount: preview?.due_amount ?? subscription.amount,
            currency: preview?.currency ?? subscription.currency,
            setupFutureUsage: 'off_session',
            excludedPaymentMethodTypes: [
              'bancontact',
              'ideal',
              'sepa_debit',
              'sofort',
            ],
            paymentMethodCreation: 'manual',
            appearance: themingPreset.stripe,
          }}
        >
          <ReactivateSubscriptionForm
            api={api}
            subscription={subscription}
            customer={customer}
            preview={preview}
            refetchPreview={refetchPreview}
            savedCards={savedCards}
            onSucceeded={handleSucceeded}
          />
        </Elements>
      )}
    </Box>
  )
}
