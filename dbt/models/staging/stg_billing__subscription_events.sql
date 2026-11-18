-- Typed, de-duplicated subscription change log with the resulting MRR after each event.
with source as (
    select * from {{ source('raw', 'subscription_events') }}
),

deduplicated as (
    select *, row_number() over (partition by event_id order by _loaded_at desc) as rn
    from source
)

select
    event_id,
    subscription_id,
    account_id,
    event_type,
    {{ to_utc('event_ts') }}                             as event_at,
    plan,
    cast(seats as integer)                               as seats,
    billing_cycle,
    cast(unit_price_usd as decimal(10, 2))               as unit_price_usd,
    cast(discount_pct as decimal(5, 2))                  as discount_pct,
    nullif(cancel_reason, '')                            as cancel_reason,
    -- MRR in effect after this event. Annual plans are normalised to a monthly amount.
    case
        when event_type = 'cancelled' then 0
        else round(
            cast(seats as integer) * cast(unit_price_usd as double)
            * (1 - cast(discount_pct as double))
            * case when billing_cycle = 'annual' then 1 - {{ var('annual_billing_discount') }} else 1 end,
            2)
    end                                                  as mrr_usd
from deduplicated
where rn = 1
