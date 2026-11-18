-- Turns the subscription change log into validity windows (SCD type 2 style):
-- each row is the state of a subscription between two consecutive events.
with events as (
    select * from {{ ref('stg_billing__subscription_events') }}
)

select
    event_id,
    subscription_id,
    account_id,
    event_type,
    plan,
    seats,
    billing_cycle,
    mrr_usd,
    cancel_reason,
    min(event_at) over (partition by subscription_id)    as subscription_started_at,
    event_at                                             as valid_from,
    lead(event_at) over (
        partition by subscription_id order by event_at, event_id
    )                                                    as valid_to
from events
