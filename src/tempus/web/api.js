// Writes to the app's JSON API, shared by the views that draft, compare and keep cards.
export const READ_ONLY = "This shared copy is read-only.";

// POST JSON; network failures come back as status 0 so callers handle one shape.
export async function post(url, body) {
  try {
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify(body) });
    return { status: res.status, body: await res.json().catch(() => null) };
  } catch {
    return { status: 0, body: null };
  }
}

export function failure(status, doing) {
  if (status === 403) return READ_ONLY;
  if (status === 0) return "Couldn't reach the app. Check it's still running, then try again.";
  return `Something went wrong ${doing}. Try again.`;
}

// Save a card spec to cards/<id>.yaml. Returns {id}, or {message} plus the card's `errors`
// (errorBlock entries) when the server refused it with a 400.
export async function keepCard(card) {
  const { status, body } = await post("/api/cards", { card });
  if (status === 201 && body?.id) return { id: body.id };
  if (status !== 400) return { message: failure(status, "keeping the card"), errors: [] };
  // FastAPI puts the reasons in `detail`: a list of card errors, or one message.
  const detail = body?.errors ?? body?.detail;
  const errors = Array.isArray(detail) ? detail.map((e) => (typeof e === "string" ? { path: "card", message: e } : e))
    : [{ path: "card", message: String(detail ?? "The card didn't pass its checks.") }];
  return { message: "", errors };
}
