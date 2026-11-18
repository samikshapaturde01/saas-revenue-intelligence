-- Support tickets normalised to UTC with consistent priority values.
with source as (
    select * from {{ source('raw', 'tickets') }}
),

deduplicated as (
    select *, row_number() over (partition by ticket_id order by _loaded_at desc) as rn
    from source
),

typed as (
    select
        ticket_id,
        account_id,
        {{ to_utc('created_at') }}                       as created_at,
        lower(trim(priority))                            as priority,
        category,
        cast(first_response_minutes as double)           as first_response_minutes,
        {{ to_utc('resolved_at') }}                      as resolved_at,
        cast(csat_score as integer)                      as csat_score
    from deduplicated
    where rn = 1
)

select
    *,
    priority in ('high', 'urgent')                       as is_high_priority,
    date_diff('minute', created_at, resolved_at) / 60.0  as resolution_hours,
    csat_score <= 2                                      as is_low_csat
from typed
