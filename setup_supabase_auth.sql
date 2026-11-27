-- Run this ONCE in Supabase Dashboard > SQL Editor > New Query.
-- Makes every extracted record belong to one signed-in user.

ALTER TABLE extracted_data
    ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES auth.users (id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_extracted_data_user
    ON extracted_data (user_id, created_at DESC);

-- The old policy let ANY client (even without login) read every row.
-- The API uses the secret key, which bypasses RLS, so it keeps working.
DROP POLICY IF EXISTS "Allow all with service key" ON extracted_data;

CREATE POLICY "Users read own rows"
    ON extracted_data FOR SELECT TO authenticated
    USING (user_id = auth.uid());

CREATE POLICY "Users delete own rows"
    ON extracted_data FOR DELETE TO authenticated
    USING (user_id = auth.uid());

-- Make the API see the new column immediately.
NOTIFY pgrst, 'reload schema';

-- OPTIONAL: records saved before this change have no owner and are hidden from everyone.
-- To give them to one account, find its id in Authentication > Users and run:
-- UPDATE extracted_data SET user_id = '<that-user-id>' WHERE user_id IS NULL;
