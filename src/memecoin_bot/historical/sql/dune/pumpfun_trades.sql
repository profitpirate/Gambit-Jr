-- Aggregate Dune route/instruction segments into one token-side transaction row.
WITH raw AS (
    SELECT
        CASE WHEN token_bought_mint_address != 'So11111111111111111111111111111111111111112'
             THEN token_bought_mint_address ELSE token_sold_mint_address END AS token_address,
        block_time AS observed_at,
        trader_id AS trader,
        CASE WHEN token_bought_mint_address != 'So11111111111111111111111111111111111111112'
             THEN 'buy' ELSE 'sell' END AS side,
        CASE WHEN token_bought_mint_address != 'So11111111111111111111111111111111111111112'
             THEN token_bought_amount ELSE token_sold_amount END AS token_amount,
        amount_usd, tx_id, block_slot
    FROM dex_solana.trades
    WHERE block_time >= TIMESTAMP '{{month_start}}'
      AND block_time < TIMESTAMP '{{month_end}}'
      AND project_program_id = '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
)
SELECT token_address, observed_at, trader, side,
       SUM(token_amount) AS token_amount, SUM(amount_usd) AS amount_usd,
       tx_id, block_slot, 'dex_solana.trades:pumpfun' AS source
FROM raw
GROUP BY token_address, observed_at, trader, side, tx_id, block_slot
ORDER BY observed_at, block_slot, tx_id, token_address, trader, side
