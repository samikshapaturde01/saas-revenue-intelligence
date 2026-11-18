select
    account_id,
    cast(date_trunc('month', issued_at) as date)                as month_start,
    count(*)                                                    as invoices,
    count(*) filter (where is_failed)                           as failed_invoices,
    count(*) filter (where required_retry)                      as retried_invoices,
    sum(amount_usd) filter (where invoice_status = 'paid')      as collected_usd
from {{ ref('stg_billing__invoices') }}
group by 1, 2
