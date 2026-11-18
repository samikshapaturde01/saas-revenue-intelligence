select
    account_id,
    cast(date_trunc('month', created_at) as date)               as month_start,
    count(*)                                                    as tickets,
    count(*) filter (where is_high_priority)                    as high_priority_tickets,
    count(*) filter (where category = 'billing')                as billing_tickets,
    count(*) filter (where category = 'bug')                    as bug_tickets,
    count(csat_score)                                           as csat_responses,
    sum(csat_score)                                             as csat_sum,
    count(*) filter (where is_low_csat)                         as low_csat_tickets,
    median(first_response_minutes)                              as median_first_response_minutes
from {{ ref('stg_support__tickets') }}
where account_id is not null
group by 1, 2
