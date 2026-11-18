-- Month-end MRR snapshot per account. One row for every month from the account's first
-- subscription to the end of the data (0 MRR while churned), so lag() works cleanly downstream.
with periods as (
    select * from {{ ref('int_subscription_periods') }}
),

spine as (
    select * from {{ ref('int_month_spine') }}
),

account_bounds as (
    select account_id, date_trunc('month', min(valid_from)) as first_month
    from periods
    group by 1
),

account_months as (
    select a.account_id, s.month_start, s.month_end
    from account_bounds as a
    join spine as s on s.month_start >= a.first_month
),

-- state of each subscription in effect at the very end of the month
state_at_month_end as (
    select
        am.account_id,
        am.month_start,
        p.subscription_id,
        p.subscription_started_at,
        p.plan,
        p.seats,
        p.billing_cycle,
        p.mrr_usd
    from account_months as am
    join periods as p
        on p.account_id = am.account_id
       and p.valid_from < am.month_end + interval 1 day
       and (p.valid_to is null or p.valid_to >= am.month_end + interval 1 day)
    where p.event_type != 'cancelled'
)

select
    am.account_id,
    am.month_start,
    coalesce(sum(s.mrr_usd), 0)                          as mrr_usd,
    coalesce(sum(s.seats), 0)                            as seats,
    max(s.plan)                                          as plan,
    max(s.billing_cycle)                                 as billing_cycle,
    max(s.subscription_id)                               as subscription_id,
    cast(date_trunc('month', max(s.subscription_started_at)) as date) as subscription_start_month,
    count(s.subscription_id) > 0                         as is_active
from account_months as am
left join state_at_month_end as s
    on s.account_id = am.account_id and s.month_start = am.month_start
group by 1, 2
