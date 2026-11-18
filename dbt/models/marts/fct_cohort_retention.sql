-- Logo and net revenue retention by monthly signup cohort.
with movements as (
    select * from {{ ref('fct_mrr_movements') }}
),

cohorts as (
    select account_id, cohort_month
    from {{ ref('dim_accounts') }}
    where cohort_month is not null
),

cohort_base as (
    select c.cohort_month, count(*) as cohort_customers, sum(m.mrr_usd) as cohort_starting_mrr_usd
    from cohorts as c
    join movements as m on m.account_id = c.account_id and m.month_start = c.cohort_month
    group by 1
),

activity as (
    select
        c.cohort_month,
        date_diff('month', c.cohort_month, m.month_start)       as months_since_start,
        count(*) filter (where m.mrr_usd > 0)                   as active_customers,
        sum(m.mrr_usd)                                          as mrr_usd
    from cohorts as c
    join movements as m on m.account_id = c.account_id
    group by 1, 2
)

select
    a.cohort_month,
    a.months_since_start,
    b.cohort_customers,
    a.active_customers,
    round(a.mrr_usd, 2)                                         as mrr_usd,
    round(a.active_customers / b.cohort_customers, 4)           as logo_retention,
    round({{ safe_divide('a.mrr_usd', 'b.cohort_starting_mrr_usd') }}, 4) as net_revenue_retention
from activity as a
join cohort_base as b using (cohort_month)
order by 1, 2
