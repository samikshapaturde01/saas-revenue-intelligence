-- Product usage per account-month. Daily averages are over all calendar days of the month
-- (days with no activity count as zero), so they are comparable across accounts.
select
    account_id,
    cast(date_trunc('month', activity_date) as date)            as month_start,
    count(*)                                                    as active_days,
    sum(active_users) / max(day(last_day(activity_date)))       as avg_daily_active_users,
    max(active_users)                                           as peak_daily_active_users,
    sum(sessions)                                               as sessions,
    sum(core_feature_events)                                    as core_feature_events,
    sum(integration_events)                                     as integration_events
from {{ ref('stg_product__usage_daily') }}
group by 1, 2
