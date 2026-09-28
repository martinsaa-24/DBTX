select
    id as order_id,
    customer as customer_id,
    store_id,
    ordered_at,
    cast(ordered_at as date) as order_date,
    cast(subtotal / 100.0 as decimal(12, 2)) as subtotal,
    cast(tax_paid / 100.0 as decimal(12, 2)) as tax_paid,
    cast(order_total / 100.0 as decimal(12, 2)) as order_total
from {{ ref('raw_orders') }}
