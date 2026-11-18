-- Labelled feature rows must have a fully observed horizon (guards against label leakage).
select f.*
from {{ ref('mart_churn_features') }} as f
cross join (select max(month_start) as last_month from {{ ref('int_month_spine') }}) as s
where f.is_label_observed
  and f.snapshot_month > s.last_month - interval {{ var('churn_horizon_months') }} month
