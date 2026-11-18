-- Starting MRR + all movements must equal ending MRR every month.
select
    month_start,
    starting_mrr_usd + new_mrr_usd + expansion_mrr_usd + reactivation_mrr_usd
        + contraction_mrr_usd + churned_mrr_usd as computed_ending,
    ending_mrr_usd
from {{ ref('metrics_mrr_monthly') }}
where abs(
    starting_mrr_usd + new_mrr_usd + expansion_mrr_usd + reactivation_mrr_usd
    + contraction_mrr_usd + churned_mrr_usd - ending_mrr_usd
) > 0.05
