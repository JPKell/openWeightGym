<!--docs
title = "Telephone and Voice status"
summary = "Configure telephone admission and inspect Voice readiness."
order = 90
covers = ["/system/voice/telephone", "/system/voice/status"]
-->
# Telephone and Voice status

Configure telephone admission and inspect Voice readiness.

## Do it

1. Follow the maintained [Telephone setup guide](/docs/operations/telephone) for accounts, public routes, caller access and simulation.
2. Open **Voice status** to inspect gateway entries, profile readiness and recent admission outcomes.
3. Use recorded rejection reasons and current node readiness when investigating a failed call.

## Limits and troubleshooting

Status reads do not start a call or prove microphone/audio-device behavior. A simulated call is distinct from a real telephone/device test. Public telephone routes and private console/browser routes have different access boundaries. Review the exact call identity before an offered end-call action.

## Related

[Voice chat](/docs/web-app/voice-chat) · [Media settings](/docs/administration/media)
