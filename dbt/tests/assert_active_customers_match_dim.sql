-- The latest month's ending customer count must match active accounts in dim_accounts.
with latest as (
    select ending_customers from {{ ref('metrics_mrr_monthly') }}
    order by month_start desc limit 1
),
dim as (
    select count(*) as active_accounts from {{ ref('dim_accounts') }} where customer_status = 'active'
)
select * from latest, dim where latest.ending_customers != dim.active_accounts
