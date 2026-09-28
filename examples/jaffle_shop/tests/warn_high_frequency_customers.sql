-- Surfaces unusually frequent customers. Warn-only and expected to return rows:
-- it exercises the rule that test warnings do not block reports.
{{ config(severity='warn') }}
select customer_id, order_count
from {{ ref('customers') }}
where order_count > 100
