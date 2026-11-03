// Only called from the private Container outbound handler, never a public route.
export async function sendAccountMail(request, env) {
  if (request.method !== "POST" || new URL(request.url).pathname !== "/send") return new Response(null, {status: 404});
  if (env.MAIL_ENABLED !== "true" || !env.EMAIL || env.MAIL_FROM !== "accounts@builtbyaadi.com") {
    return Response.json({error: "mail_unavailable"}, {status: 503});
  }
  try {
    const text = await request.text();
    if (text.length > 2048) return new Response(null, {status: 413});
    const {email, purpose, code} = JSON.parse(text);
    if (typeof email !== "string" || email.length > 254 || !/^[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+$/.test(email)
        || !["verify", "reset"].includes(purpose) || typeof code !== "string" || !/^[A-Za-z0-9_-]{6,256}$/.test(code)) {
      return Response.json({error: "invalid_request"}, {status: 400});
    }
    const verifying = purpose === "verify";
    const result = await env.EMAIL.send({
      to: email, from: env.MAIL_FROM,
      subject: verifying ? "Verify your Built by Aadi account" : "Reset your Built by Aadi password",
      text: `${verifying ? "Your account verification code" : "Your password reset code"}: ${code}\n\nEnter this code on the Built by Aadi page where you requested it. If you did not request this, ignore this email. Never share this code or your Canvas token.`,
    });
    if (!result?.messageId) throw new Error("mail_not_confirmed");
    return Response.json({sent: true}, {headers: {"Cache-Control": "no-store"}});
  } catch {
    // No provider errors, recipient addresses or one-time codes enter responses/logs.
    return Response.json({error: "mail_not_confirmed"}, {status: 503});
  }
}
