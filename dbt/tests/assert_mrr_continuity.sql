-- Each month's starting MRR must equal the previous month's ending MRR.
with m as (
    select
        month_start,
        starting_mrr_usd,
        lag(ending_mrr_usd) over (order by month_start) as prev_ending
    from {{ ref('metrics_mrr_monthly') }}
)
select * from m
where prev_ending is not null and abs(starting_mrr_usd - prev_ending) > 0.05
