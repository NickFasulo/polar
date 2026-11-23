import { type schemas } from '@polar-sh/client'
import { formatCurrency } from '@polar-sh/currency'
import { Text } from '@polar-sh/orbit'
import { Box } from '@polar-sh/orbit/Box'
import { PortalRow, PortalRows, PortalText } from '../PortalPrimitives'
import { ReactivationCard } from './ReactivationCard'

export const ReactivationSummary = ({
  subscription,
  preview,
}: {
  subscription: schemas['CustomerSubscription']
  preview: schemas['SubscriptionReactivationPreview'] | undefined
}) => {
  const format = (amount: number) =>
    formatCurrency('compact')(
      amount,
      preview?.currency ?? subscription.currency,
    )

  return (
    <ReactivationCard rowGap="2xl">
      <Box flexDirection="column" rowGap="xs">
        <Text variant="heading-l" as="h1">
          {format(preview?.due_amount ?? subscription.amount)}
        </Text>
        <PortalText tone="secondary">
          {preview ? 'Due today' : 'Due today, plus tax'}
        </PortalText>
      </Box>

      <Box flexDirection="column" rowGap="l">
        <PortalRows>
          {preview ? (
            <>
              <PortalRow
                label="Subtotal"
                value={format(preview.subtotal_amount)}
                numeric
              />
              {preview.discount_amount > 0 && (
                <PortalRow
                  label="Discount"
                  value={format(-preview.discount_amount)}
                  numeric
                />
              )}
              <PortalRow
                label="Tax"
                value={format(preview.tax_amount)}
                numeric
              />
              {preview.applied_balance_amount !== 0 && (
                <PortalRow
                  label="Account balance"
                  value={format(preview.applied_balance_amount)}
                  numeric
                />
              )}
            </>
          ) : (
            <PortalRow
              label="Subtotal"
              value={format(subscription.amount)}
              numeric
            />
          )}
        </PortalRows>
        {preview ? (
          <PortalRow
            label="Total due today"
            value={format(preview.due_amount)}
            emphasis
            numeric
          />
        ) : (
          <PortalText tone="secondary" className="text-sm">
            Tax is calculated once you enter your billing address.
          </PortalText>
        )}
      </Box>
    </ReactivationCard>
  )
}
