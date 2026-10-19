(() => {
  const TOKEN_KEY = "papermind_token";
  const USER_KEY = "papermind_user";
  const form = document.getElementById("authForm");
  const error = document.getElementById("authError");
  const submit = document.getElementById("authSubmit");
  const isSignup = form?.dataset.mode === "signup";

  if (localStorage.getItem(TOKEN_KEY)) {
    fetch("/auth/me", { headers: { Authorization: `Bearer ${localStorage.getItem(TOKEN_KEY)}` } })
      .then(response => {
        if (response.ok) {
          window.location.replace("/");
        } else {
          localStorage.removeItem(TOKEN_KEY);
          localStorage.removeItem(USER_KEY);
        }
      })
      .catch(() => {});
  }

  function showError(message) { error.textContent = message; error.hidden = false; }
  function messageFor(payload, fallback) { return typeof payload?.detail === "string" ? payload.detail : fallback; }

  form?.addEventListener("submit", async event => {
    event.preventDefault();
    error.hidden = true;
    const email = document.getElementById("email").value.trim();
    const password = document.getElementById("password").value;
    const body = { email, password };
    if (isSignup) body.name = document.getElementById("name").value.trim();
    if (!email || !password || (isSignup && !body.name)) return showError("Please complete every field.");

    submit.disabled = true;
    submit.textContent = isSignup ? "Creating account..." : "Logging in...";
    try {
      const response = await fetch(isSignup ? "/auth/signup" : "/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.token || !payload.user) throw new Error(messageFor(payload, "We could not complete that request."));
      localStorage.setItem(TOKEN_KEY, payload.token);
      localStorage.setItem(USER_KEY, JSON.stringify(payload.user));
      window.location.replace("/");
    } catch (exception) {
      showError(exception.message || "Unable to contact the server.");
    } finally {
      submit.disabled = false;
      submit.textContent = isSignup ? "Create account" : "Log in";
    }
  });
})();
