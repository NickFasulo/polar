import { getServerSideAPI } from '@/utils/client/serverside'
import { getOrganizationOrNotFound } from '@/utils/customerPortal'
import { redirect } from 'next/navigation'
import ReactivatePage from './ReactivatePage'

export { generateMetadata } from '../page'

export default async function Page(props: {
  params: Promise<{ organization: string; id: string }>
  searchParams: Promise<{
    customer_session_token?: string
    member_session_token?: string
    locale?: string
  }>
}) {
  const resolvedSearchParams = await props.searchParams
  const { customer_session_token, member_session_token, ...searchParams } =
    resolvedSearchParams
  const params = await props.params
  const token = customer_session_token ?? member_session_token
  const api = await getServerSideAPI(token)
  const { organization } = await getOrganizationOrNotFound(
    api,
    params.organization,
    searchParams,
  )

  const {
    data: subscription,
    error,
    response,
  } = await api.GET('/v1/customer-portal/subscriptions/{id}', {
    params: { path: { id: params.id } },
    cache: 'no-cache',
    next: { tags: [`customer_portal`] },
  })

  if (response.status === 401) {
    redirect(`/${organization.slug}/portal/request`)
  }

  if (error) {
    throw error
  }

  return (
    <ReactivatePage
      organization={organization}
      subscription={subscription}
      customerSessionToken={token as string}
    />
  )
}
