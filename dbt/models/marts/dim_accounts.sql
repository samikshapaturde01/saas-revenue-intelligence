-- One row per customer account with firmographics and lifecycle / revenue attributes.
with accounts as (
    select * from {{ ref('stg_crm__accounts') }}
),

movements as (
    select * from {{ ref('fct_mrr_movements') }}
),

lifecycle as (
    select
        account_id,
        min(month_start) filter (where movement_type = 'new')               as cohort_month,
        max(month_start) filter (where movement_type = 'churn')             as last_churn_month,
        count(*) filter (where movement_type = 'churn')                     as churn_count,
        max(mrr_usd)                                                        as peak_mrr_usd,
        arg_max(mrr_usd, month_start)                                       as current_mrr_usd,
        arg_max(plan, month_start)                                          as current_plan,
        arg_max(billing_cycle, month_start)                                 as current_billing_cycle,
        arg_max(seats, month_start)                                         as current_seats
    from movements
    group by 1
),

last_cancel as (
    select account_id, arg_max(cancel_reason, event_at) as last_cancel_reason
    from {{ ref('stg_billing__subscription_events') }}
    where event_type = 'cancelled'
    group by 1
),

revenue as (
    select account_id, sum(amount_usd) filter (where invoice_status = 'paid') as lifetime_revenue_usd
    from {{ ref('stg_billing__invoices') }}
    group by 1
)

select
    a.account_id,
    a.company_name,
    a.industry,
    a.company_size,
    a.company_size_rank,
    a.country_code,
    a.acquisition_channel,
    a.signup_date,
    l.cohort_month,
    case
        when l.account_id is null then 'never_activated'
        when l.current_mrr_usd > 0 then 'active'
        else 'churned'
    end                                                                     as customer_status,
    coalesce(l.current_mrr_usd, 0)                                          as current_mrr_usd,
    l.current_plan,
    l.current_billing_cycle,
    l.current_seats,
    l.peak_mrr_usd,
    l.last_churn_month,
    case when l.current_mrr_usd = 0 then c.last_cancel_reason end           as churn_reason,
    coalesce(l.churn_count, 0)                                              as churn_count,
    round(coalesce(r.lifetime_revenue_usd, 0), 2)                           as lifetime_revenue_usd
from accounts as a
left join lifecycle as l using (account_id)
left join last_cancel as c using (account_id)
left join revenue as r using (account_id)
