{#- For `dbtx run-operation log_store_count`: logs how many stores the warehouse holds. -#}
{% macro log_store_count() %}
  {% set result = run_query("select count(*) from " ~ ref("raw_stores")) %}
  {% if execute %}
    {{ log("jaffle_shop: " ~ result.columns[0].values()[0] ~ " stores", info=True) }}
  {% endif %}
{% endmacro %}
