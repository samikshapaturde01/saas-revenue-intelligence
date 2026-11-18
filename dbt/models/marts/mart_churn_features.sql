-- ML feature table: one row per active account per month-end snapshot.
-- Every feature only uses information available at the snapshot month's end (no leakage);
-- the label looks forward `churn_horizon_months` months.
{% set horizon = var('churn_horizon_months') %}

with mrr as (
    select * from {{ ref('int_account_mrr_monthly') }}
),

base as (
    select
        m.account_id,
        m.month_start                                           as snapshot_month,
        m.mrr_usd,
        m.seats,
        m.plan,
        m.billing_cycle,
        m.subscription_start_month,
        coalesce(u.active_days, 0)                              as active_days,
        coalesce(u.avg_daily_active_users, 0)                   as avg_daily_active_users,
        coalesce(u.sessions, 0)                                 as sessions,
        coalesce(u.core_feature_events, 0)                      as core_feature_events,
        coalesce(u.integration_events, 0)                       as integration_events,
        coalesce(s.tickets, 0)                                  as tickets,
        coalesce(s.high_priority_tickets, 0)                    as high_priority_tickets,
        coalesce(s.billing_tickets, 0)                          as billing_tickets,
        coalesce(s.low_csat_tickets, 0)                         as low_csat_tickets,
        coalesce(s.csat_responses, 0)                           as csat_responses,
        coalesce(s.csat_sum, 0)                                 as csat_sum,
        coalesce(b.failed_invoices, 0)                          as failed_invoices
    from mrr as m
    left join {{ ref('int_usage_monthly') }}   as u using (account_id, month_start)
    left join {{ ref('int_support_monthly') }} as s using (account_id, month_start)
    left join {{ ref('int_billing_monthly') }} as b using (account_id, month_start)
),

windowed as (
    select
        *,
        avg(avg_daily_active_users) over prev_3          as prev_3m_avg_dau,
        lag(mrr_usd, 3) over w                           as mrr_3m_ago,
        lag(seats, 3) over w                             as seats_3m_ago,
        sum(tickets) over last_3                         as tickets_3m,
        sum(high_priority_tickets) over last_3           as high_priority_tickets_3m,
        sum(billing_tickets) over last_3                 as billing_tickets_3m,
        sum(low_csat_tickets) over last_3                as low_csat_tickets_3m,
        sum(csat_sum) over last_3                        as csat_sum_3m,
        sum(csat_responses) over last_3                  as csat_responses_3m,
        sum(failed_invoices) over last_3                 as failed_invoices_3m,
        min(mrr_usd) over next_h                         as min_future_mrr_usd,
        count(*) over next_h                             as future_months_observed
    from base
    window
        w      as (partition by account_id order by snapshot_month),
        prev_3 as (partition by account_id order by snapshot_month rows between 3 preceding and 1 preceding),
        last_3 as (partition by account_id order by snapshot_month rows between 2 preceding and current row),
        next_h as (partition by account_id order by snapshot_month
                   rows between 1 following and {{ horizon }} following)
)

select
    {{ dbt.concat(["w.account_id", "'-'", "strftime(w.snapshot_month, '%Y%m')"]) }} as feature_row_id,
    w.account_id,
    w.snapshot_month,

    -- firmographics
    a.industry,
    a.company_size_rank,
    a.acquisition_channel,
    a.country_code,

    -- commercial
    date_diff('month', a.cohort_month, w.snapshot_month)                 as tenure_months,
    w.plan,
    w.billing_cycle = 'annual'                                           as is_annual,
    case when w.billing_cycle = 'annual'
         then date_diff('month', w.subscription_start_month, w.snapshot_month) % 12
    end                                                                  as months_into_annual_term,
    w.seats,
    w.mrr_usd,
    coalesce({{ safe_divide('w.mrr_usd - w.mrr_3m_ago', 'w.mrr_3m_ago') }}, 0) as mrr_change_3m_pct,
    coalesce(w.seats - w.seats_3m_ago, 0)                                as seats_change_3m,

    -- product engagement
    w.active_days,
    round({{ safe_divide('w.avg_daily_active_users', 'w.seats') }}, 4)   as seat_utilization,
    round({{ safe_divide('w.sessions', 'w.seats') }}, 3)                 as sessions_per_seat,
    round({{ safe_divide('w.core_feature_events', 'w.sessions') }}, 3)   as core_events_per_session,
    w.integration_events > 0                                             as uses_integrations,
    round(coalesce({{ safe_divide('w.avg_daily_active_users', 'w.prev_3m_avg_dau') }}, 1), 4)
                                                                         as dau_trend_vs_prev_3m,

    -- support experience
    w.tickets_3m,
    w.high_priority_tickets_3m,
    w.billing_tickets_3m,
    w.low_csat_tickets_3m,
    round({{ safe_divide('w.csat_sum_3m', 'w.csat_responses_3m') }}, 3)  as avg_csat_3m,

    -- billing health
    w.failed_invoices_3m,

    -- label
    w.future_months_observed = {{ horizon }}                             as is_label_observed,
    case when w.future_months_observed = {{ horizon }}
         then w.min_future_mrr_usd = 0 end                               as churned_within_horizon
from windowed as w
join {{ ref('dim_accounts') }} as a using (account_id)
where w.mrr_usd > 0
