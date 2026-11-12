/** User shape — mirrors backend MeOut. Identity comes from a verified
 * forwarded/OIDC token. Machine accounts are denied in the public core. */
export interface User {
  id: string
  email: string
  name: string | null
  avatar_url: string | null
  is_admin: boolean
  created_at: string
  last_sign_in_at: string | null
  /** Reserved for a future explicit machine authorization adapter. */
  actor: string | null
  /** Reserved for a future explicit machine authorization adapter. */
  gateway_scope: string | null
}
