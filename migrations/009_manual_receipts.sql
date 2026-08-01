-- Manual pantry entry: a receipt with no photograph.
--
-- Not everything arrives with a receipt — a farmers market, a gift, or the
-- baseline stocktake you do on day one. Rather than invent a second way into
-- the inventory, a manual batch *is* a receipt that happens to have no image.
-- That reuses the review screen, which is the feature's whole safety
-- property, and keeps the one-mutation-per-trip undo behaviour unchanged.
--
-- `source` is a real column rather than an inference, because the obvious
-- inference is wrong: `image_path IS NULL` also describes a photographed
-- receipt whose picture was pruned thirty days after confirmation.
--
-- `image_sha256` stays NOT NULL UNIQUE. A manual batch fills it with a
-- synthetic `manual:<uuid4>` key: there is no image to hash, and each batch
-- is genuinely distinct, so unlike a re-photographed receipt it must never
-- collide with an earlier one.

ALTER TABLE receipts ADD COLUMN source TEXT NOT NULL DEFAULT 'photo';  -- photo|manual
