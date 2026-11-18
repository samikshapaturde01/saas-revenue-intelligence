-- Invoices converted to USD using monthly FX rates.
with source as (
    select * from {{ source('raw', 'invoices') }}
),

deduplicated as (
    select *, row_number() over (partition by invoice_id order by _loaded_at desc) as rn
    from source
),

typed as (
    select
        invoice_id,
        account_id,
        subscription_id,
        {{ to_utc('issued_at') }}                        as issued_at,
        cast(due_at as date)                             as due_date,
        currency,
        {{ cents_to_dollars('amount_cents') }}           as amount_local,
        status                                           as invoice_status,
        cast(attempt_count as integer)                   as attempt_count,
        {{ to_utc('paid_at') }}                          as paid_at
    from deduplicated
    where rn = 1
)

select
    typed.*,
    round(typed.amount_local * fx.usd_per_unit, 2)       as amount_usd,
    invoice_status = 'failed'                            as is_failed,
    attempt_count > 1                                    as required_retry
from typed
left join {{ ref('fx_rates_monthly') }} as fx
    on fx.currency = typed.currency
   and fx.month = date_trunc('month', typed.issued_at)
