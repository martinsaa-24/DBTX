with item_counts as (
    select
        order_id,
        count(*) as item_count,
        count(*) filter (where product_type = 'jaffle') as jaffle_count,
        count(*) filter (where product_type = 'beverage') as beverage_count,
        sum(product_price) as items_subtotal
    from {{ ref('int_order_items_priced') }}
    group by 1
)

select
    orders.order_id,
    orders.customer_id,
    orders.store_id,
    orders.ordered_at,
    orders.order_date,
    orders.subtotal,
    orders.tax_paid,
    orders.order_total,
    item_counts.item_count,
    item_counts.jaffle_count,
    item_counts.beverage_count,
    item_counts.items_subtotal
from {{ ref('stg_orders') }} as orders
join item_counts using (order_id)
