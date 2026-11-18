-- Account-month MRR movements: the building block of every SaaS revenue metric.
with mrr as (
    select
        *,
        lag(mrr_usd, 1, 0) over (partition by account_id order by month_start) as previous_mrr_usd,
        -- has this account ever had MRR before this month?
        coalesce(
            max(case when mrr_usd > 0 then 1 else 0 end) over (
                partition by account_id order by month_start
                rows between unbounded preceding and 1 preceding
            ), 0) = 1                                                         as had_prior_mrr
    from {{ ref('int_account_mrr_monthly') }}
),

classified as (
    select
        account_id,
        month_start,
        plan,
        billing_cycle,
        seats,
        previous_mrr_usd,
        mrr_usd,
        mrr_usd - previous_mrr_usd                                            as mrr_change_usd,
        case
            when previous_mrr_usd = 0 and mrr_usd > 0 and not had_prior_mrr  then 'new'
            when previous_mrr_usd = 0 and mrr_usd > 0                        then 'reactivation'
            when previous_mrr_usd > 0 and mrr_usd = 0                        then 'churn'
            when mrr_usd > previous_mrr_usd                                  then 'expansion'
            when mrr_usd < previous_mrr_usd                                  then 'contraction'
            else 'retained'
        end                                                                   as movement_type
    from mrr
    where mrr_usd > 0 or previous_mrr_usd > 0
)

select
    {{ dbt.concat(["account_id", "'-'", "strftime(month_start, '%Y%m')"]) }}  as account_month_id,
    *,
    case when movement_type = 'new'          then mrr_change_usd else 0 end  as new_mrr_usd,
    case when movement_type = 'expansion'    then mrr_change_usd else 0 end  as expansion_mrr_usd,
    case when movement_type = 'contraction'  then mrr_change_usd else 0 end  as contraction_mrr_usd,
    case when movement_type = 'churn'        then mrr_change_usd else 0 end  as churned_mrr_usd,
    case when movement_type = 'reactivation' then mrr_change_usd else 0 end  as reactivation_mrr_usd
from classified
