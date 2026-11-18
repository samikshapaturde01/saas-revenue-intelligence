-- Calendar of month starts covering the full history of the business.
with bounds as (
    select
        date_trunc('month', min(event_at)) as first_month,
        date_trunc('month', max(event_at)) as last_month
    from {{ ref('stg_billing__subscription_events') }}
)

select
    cast(month_start as date)                                       as month_start,
    cast(month_start + interval 1 month - interval 1 day as date)   as month_end
from bounds,
    unnest(generate_series(first_month, last_month, interval 1 month)) as t(month_start)
