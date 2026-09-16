def trunc(n): if (. | length) > n then .[0:n] + "…" else . end;

if .type == "assistant" then
  (.message.content // [])[] |
  if .type == "text" then
    "\n[36m[claude][0m " + .text
  elif .type == "tool_use" then
    "\n[33m[tool][0m " + .name + " " + (.input | tostring | trunc(200))
  else empty
  end
elif .type == "user" then
  (.message.content // [])[] |
  if .type == "tool_result" then
    "[90m[result][0m " + ((.content | if type == "string" then . else tostring end) | trunc(300))
  else empty
  end
elif .type == "result" then
  "\n[32m[done][0m " + (.result // "") + "  (cost $" + (.total_cost_usd | tostring) + ")"
else empty
end
