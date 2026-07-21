TPCDS_SCHEMA = {
    "tpcds": {
        "catalog_sales": ["cs_item_sk", "cs_order_number", "cs_ext_list_price"],
        "customer": [
            "c_customer_sk",
            "c_current_addr_sk",
            "c_first_name",
            "c_last_name",
        ],
        "customer_address": ["ca_address_sk", "ca_city", "ca_state", "ca_zip"],
        "date_dim": ["d_date_sk", "d_year", "d_moy", "d_week_seq"],
        "item": [
            "i_item_sk",
            "i_item_id",
            "i_category",
            "i_brand",
            "i_color",
            "i_product_name",
        ],
        "store": ["s_store_sk", "s_store_name", "s_state", "s_zip"],
        "store_returns": ["sr_item_sk", "sr_ticket_number", "sr_customer_sk"],
        "store_sales": [
            "ss_item_sk",
            "ss_ticket_number",
            "ss_customer_sk",
            "ss_store_sk",
            "ss_sold_date_sk",
            "ss_sales_price",
        ],
        "web_sales": ["ws_item_sk", "ws_sold_date_sk", "ws_ext_sales_price"],
    }
}
