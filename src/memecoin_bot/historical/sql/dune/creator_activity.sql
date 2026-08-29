SELECT
    signer AS creator,
    block_time AS observed_at,
    signature AS tx_id,
    block_slot,
    success,
    'solana.transactions:pumpfun' AS source
FROM solana.transactions
WHERE block_time >= TIMESTAMP '{{month_start}}'
  AND block_time < TIMESTAMP '{{month_end}}'
  AND contains(account_keys, '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P')
ORDER BY block_time, block_slot, signature
