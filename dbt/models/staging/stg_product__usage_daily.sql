-- One row per account per day. Upstream re-sends some days; keep a single copy.
with source as (
    select * from {{ source('raw', 'usage_daily') }}
),

deduplicated as (
    select
        *,
        row_number() over (
            partition by account_id, cast(cast(activity_date as timestamp) as date)
            order by _loaded_at desc
        ) as rn
    from source
)

select
    account_id,
    cast(cast(activity_date as timestamp) as date)       as activity_date,
    cast(active_users as integer)                        as active_users,
    cast(sessions as integer)                            as sessions,
    cast(core_feature_events as integer)                 as core_feature_events,
    cast(integration_events as integer)                  as integration_events
from deduplicated
where rn = 1
