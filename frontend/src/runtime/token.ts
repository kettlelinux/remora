// The backend auth token. Only the CDP bootstrap hands it out: it sets window.deckyAuthToken (and
// remoraAuthToken), again after every backend restart. There is no HTTP route for it.
export function currentToken(): string | undefined {
  return window.deckyAuthToken || window.remoraAuthToken;
}
