"""Reviewed GET-only meter validation; no credentials or account defaults."""
from datetime import datetime, timedelta
from decimal import Decimal

def require(ok,message):
    if not ok:raise ValueError(message)

def number(value):
    require(type(value) in (int,float) and Decimal(str(value)).is_finite() and value>=0,'Invalid provider meter')
    return value

def units(value):
    require(type(value) is int and value>=0,'Provider units must be nonnegative integers')
    return value

def parse_billing_proof(proof,binding,now):
    ACCOUNT = binding['accountId']
    RESOURCE = binding['usageResource']
    require(isinstance(ACCOUNT,str) and bool(ACCOUNT) and isinstance(RESOURCE,str) and bool(RESOURCE) and binding['monthlyAllowance']==5000,'Unreviewed account binding')
    resources = binding.get('usageResources', [RESOURCE])
    require(isinstance(resources, list) and 1 <= len(resources) <= 3 and all(isinstance(r,str) and r for r in resources)
            and len(set(resources)) == len(resources) and RESOURCE in resources, 'Unreviewed shared account resources')
    resources = set(resources)
    require(proof['credentialSha256']==binding['credentialSha256'],'Credential binding changed')
    responses=proof['responses']
    for name in ('status','balance','pricing','usage'):
        require(responses[name]['status']==200,'Provider billing read failed: '+name)
    require(responses['status']['data']['customer']==ACCOUNT and responses['status']['data']['status']=='active','Provider account identity/status changed')
    balance=responses['balance']['data']
    require(Decimal(str(balance['balance'])).is_finite() and Decimal(str(balance['balance']))>=0,'Provider balance is invalid or negative')
    require(number(balance['pending_costs'])==0,'Unexpected pending paid usage requires review')
    plans=[p for p in responses['pricing']['data']['plans'] if p.get('is_current') is True]
    require(len(plans)==1 and plans[0]['id']==binding['pricingPlanId'],'Pricing plan changed')
    require(plans[0]['product_prices']['data_api']['price_groups'][0]['prices']['record']==1.5,'Scraper pricing changed')
    all_usage=responses['usage']['data']
    require(set(all_usage)=={ACCOUNT},'Usage account mismatch')
    usage=all_usage[ACCOUNT]
    require(usage['customer_id']==ACCOUNT and usage['from']==now.strftime('%Y-%m-01T00:00:00.000Z'),'Usage period mismatch')
    require(usage['to']==(now+timedelta(days=1)).strftime('%Y-%m-%dT00:00:00.000Z'),'Usage end date mismatch')
    cutoff=datetime.fromisoformat(usage['last_update_ts'].replace('Z','+00:00'))
    require(cutoff.tzinfo is not None and timedelta(0)<=now-cutoff<=timedelta(hours=2),'Provider rollup is stale or future-dated')
    require(cutoff.strftime('%Y-%m')==now.strftime('%Y-%m'),'Wrong usage month')
    records=0
    resource_records={}
    for resource,metrics in usage['data'].items():
        for key,values in metrics.items():
            require(isinstance(values,list) and len(values)==now.day,'Incomplete daily provider rollup')
            total=sum(units(v) for v in values)
            if resource not in resources or key not in {'rows_initial_billable','sets_initial_billable'}:
                require(total==0,'Unsupported shared-credit consumption detected')
        daily=sum(metrics.get('rows_initial_billable',[]))
        if resource in resources:
            summaries=usage['sums'].get(resource,{})
            for window in ('back_m0','custom'):
                require(units(summaries.get(window,{}).get('rows_initial_billable',0))==daily,'Provider daily/month totals disagree')
            records+=daily
            resource_records[resource]=daily
    # Check every totals-only resource too: the daily array cannot omit usage.
    for resource,windows in usage['sums'].items():
        for window,metrics in windows.items():
            for key,value in metrics.items():
                number(value)
                if resource not in resources or key not in {'rows_initial_billable','sets_initial_billable'}:
                    require(value==0,'Unsupported billed product/meter detected')
                if resource in resources and key=='rows_initial_billable' and window in {'back_m0','custom'}:
                    require(units(value)==resource_records.get(resource,0),'Provider totals missing daily rows')
    for resource,metrics in usage['snapshots'].items():
        for key,samples in metrics.items():
            require(isinstance(samples,list),'Invalid live usage series')
            for sample in samples:
                require(isinstance(sample,list) and len(sample)==2,'Invalid live usage sample')
                value=number(sample[1])
                if resource in resources and key=='rows_initial_billable':
                    units(value)
                    require(value<=resource_records.get(resource,0),'Live usage is ahead of the settled rollup')
                else:
                    require(value==0,'Unsupported live product consumption detected')
    datasets = binding.get('datasetUsageResources', {})
    require(isinstance(datasets, dict) and all(isinstance(k, str) and v in resources for k, v in datasets.items()), 'Invalid dataset meter binding')
    require(len(set(datasets.values())) == len(datasets), 'Dataset meters must not be counted twice')
    scope = {'account_ref': ACCOUNT, 'month': cutoff.strftime('%Y-%m'),
        'usage_as_of': cutoff.isoformat(), 'datasets': {dataset: {
            'month': resource_records.get(resource, 0),
            'daily': usage['data'].get(resource, {}).get('rows_initial_billable', [0] * now.day),
        } for dataset, resource in datasets.items()}}
    # Missing dataset bindings can never settle historical dataset reservations.
    scope['complete'] = bool(datasets and set(datasets.values()) == resources)
    return {'account_ref':ACCOUNT,'credential_sha256':binding['credentialSha256'],'consumers_isolated':binding.get('consumersIsolated') is True,'month':cutoff.strftime('%Y-%m'),'usage_as_of':cutoff.isoformat(),
        'billing_scope': scope,
        'billable_records':records,'verified_monthly_allowance':5000,'balance':balance['balance'],'active':True,
        'evidence':f'Bound account {ACCOUNT}; GET /customer/bw daily rows agree with current-month/custom totals; plan {binding["pricingPlanId"]}; shared/unsupported meters zero; initial allowance verified in owner panel. Conservative reservations across the previous account remain in the ledger.'}

