"""A Facebook trigger requires fresh proof covering all enabled account meters."""
from datetime import datetime
from app.config import settings
from app.intelligence.credit_control import locked_account
from app.intelligence.trial_usage import load_binding,read_billing_evidence

async def refresh_facebook_billing(db,now,dataset_id):
    binding=load_binding()
    # Exact resource-to-dataset binding is established by the private operator,
    # never inferred from a truncated provider resource name in a response.
    mapping=binding.get('datasetUsageResources',{})
    resource=mapping.get(dataset_id)
    if not resource or resource not in binding.get('usageResources',[]):
        raise ValueError('Facebook billing resource is not explicitly bound.')
    proof=await read_billing_evidence(settings.BRIGHTDATA_API_TOKEN,binding,now)
    cutoff=datetime.fromisoformat(proof['usage_as_of'])
    account=await locked_account(db)
    if account.paused_reason:raise ValueError('Account billing is paused.')
    if account.usage_as_of and cutoff<account.usage_as_of:raise ValueError('Older billing cutoff rejected.')
    if account.provider_month==proof['month'] and (account.provider_billable_records or 0)>proof['billable_records']:
        raise ValueError('Provider usage cannot decrease.')
    account.provider_month=proof['month'];account.provider_billable_records=proof['billable_records']
    account.provider_balance=proof['balance'];account.provider_active=proof['active']
    account.verified_monthly_allowance=proof['verified_monthly_allowance']
    account.usage_as_of=cutoff;account.verified_at=now;account.evidence=proof['evidence']
    await db.commit()
