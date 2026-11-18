-- Company-level monthly SaaS metrics: the MRR waterfall, churn rates and retention.
with movements as (
    select * from {{ ref('fct_mrr_movements') }}
),

monthly as (
    select
        month_start,
        sum(previous_mrr_usd)                                      as starting_mrr_usd,
        sum(new_mrr_usd)                                           as new_mrr_usd,
        sum(expansion_mrr_usd)                                     as expansion_mrr_usd,
        sum(reactivation_mrr_usd)                                  as reactivation_mrr_usd,
        sum(contraction_mrr_usd)                                   as contraction_mrr_usd,
        sum(churned_mrr_usd)                                       as churned_mrr_usd,
        sum(mrr_usd)                                               as ending_mrr_usd,
        count(*) filter (where previous_mrr_usd > 0)               as starting_customers,
        count(*) filter (where movement_type = 'new')              as new_customers,
        count(*) filter (where movement_type = 'reactivation')     as reactivated_customers,
        count(*) filter (where movement_type = 'churn')            as churned_customers,
        count(*) filter (where mrr_usd > 0)                        as ending_customers
    from movements
    group by 1
)

select
    month_start,
    round(starting_mrr_usd, 2)                                     as starting_mrr_usd,
    round(new_mrr_usd, 2)                                          as new_mrr_usd,
    round(expansion_mrr_usd, 2)                                    as expansion_mrr_usd,
    round(reactivation_mrr_usd, 2)                                 as reactivation_mrr_usd,
    round(contraction_mrr_usd, 2)                                  as contraction_mrr_usd,
    round(churned_mrr_usd, 2)                                      as churned_mrr_usd,
    round(ending_mrr_usd, 2)                                       as ending_mrr_usd,
    round(ending_mrr_usd * 12, 2)                                  as ending_arr_usd,
    starting_customers,
    new_customers,
    reactivated_customers,
    churned_customers,
    ending_customers,
    round({{ safe_divide('ending_mrr_usd', 'ending_customers') }}, 2)              as arpa_usd,
    round({{ safe_divide('churned_customers', 'starting_customers') }}, 4)         as logo_churn_rate,
    round({{ safe_divide('-(churned_mrr_usd + contraction_mrr_usd)', 'starting_mrr_usd') }}, 4)
                                                                                    as gross_mrr_churn_rate,
    round({{ safe_divide('starting_mrr_usd + expansion_mrr_usd + contraction_mrr_usd + churned_mrr_usd',
                         'starting_mrr_usd') }}, 4)                                as net_mrr_retention_rate,
    round({{ safe_divide('ending_mrr_usd - starting_mrr_usd', 'starting_mrr_usd') }}, 4)
                                                                                    as mrr_growth_rate
from monthly
order by month_start
