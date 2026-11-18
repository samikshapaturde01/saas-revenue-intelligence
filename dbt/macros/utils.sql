{#- Normalise an ISO-8601 string with any UTC offset to a naive UTC timestamp. -#}
{% macro to_utc(column) -%}
    (cast({{ column }} as timestamptz) at time zone 'UTC')
{%- endmacro %}

{% macro cents_to_dollars(column, scale=2) -%}
    round(cast({{ column }} as decimal(18, 2)) / 100, {{ scale }})
{%- endmacro %}

{#- Safe division that returns null instead of erroring / dividing by zero. -#}
{% macro safe_divide(numerator, denominator) -%}
    ({{ numerator }}) / nullif(({{ denominator }}), 0)
{%- endmacro %}
