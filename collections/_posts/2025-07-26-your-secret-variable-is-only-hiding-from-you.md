---
layout:     post
title:      Your Secret Variable Is Only Hiding From You
date:       2025-07-26
description:    Marking a Postman variable "secret" masks it with dots in the UI. It does not encrypt it, and it does not stop the app from resolving it to plaintext and syncing it to Postman's servers. A researcher caught the client shipping resolved secrets to analytics endpoints. Whether that exact bug still lives, the structural truth does, because a cloud tool that resolves your secret already has it.
categories: security api-keys secrets postman devsecops privacy
---

There is a lock icon next to the environment variable, and the value shows as a row of dots, and that little bit of UI does a lot of quiet work to make you feel safe. You marked it secret. Now it is protected. That is the story the interface tells, and it is true in exactly one narrow sense and false in every sense that matters.

Here is the claim that should bother you. A researcher put a proxy in front of the Postman client, bypassed the certificate pinning, and watched what the app actually sent home. Inside the traffic were resolved request URLs, the version of the URL after the variables are substituted in, with the secret values inlined in plaintext, being posted to Postman's own analytics endpoints. Not the masked value. The real one. For variables the UI was showing as protected dots. The suggested mitigation was to blackhole two `bifrost` analytics domains in your `/etc/hosts` so the client could not reach them, which tells you everything about where the data was going.

I want to be careful here, because this is one person's capture and it is a few years old, and Postman may well have changed this specific behavior since. Do not take my summary or their screenshot as the current state of the world. But also do not let "they probably fixed it" put you back to sleep, because the specific bug is not the lesson. The lesson is structural, and the structure has not changed.

## The problem

Start with what "secret" means in a tool like this, because it does not mean what you think.

Marking a variable secret is masking. It is a privacy screen. It stops the person sitting behind you, and the recording of your screen share, and the screenshot in the bug report, from showing your token in the clear. That is a real and useful thing. It is also the entire thing. Masking is a display property, applied at the last moment before pixels, and it has nothing to do with how the value is stored, resolved, or transmitted.

And it cannot have anything to do with resolution, because resolution is the whole point of the variable. When you fire the request, Postman has to turn `{{api_key}}` back into the actual key to put it in the header, otherwise the request does not work. So at request time the plaintext secret exists, in memory, in the client, fully resolved. The dots were never on the value. The dots were on one view of the value. Everything downstream of resolution, the request itself, any telemetry, any crash log, any sync, is working with the real string.

Now add the cloud. Postman is not a local tool that happens to have a login, it is a cloud product, and your workspaces sync. That sync is not a leak, it is the feature, you asked for it so your collections follow you across machines and teammates. But read what it means plainly: the moment a workspace syncs, everything in it, including the environment with the resolved secrets, exists on Postman's servers in a form Postman can read. It has to be readable, because they render it back to you in the next session. Encrypted-at-rest does not save you here, because they hold the key, they must, that is what makes it come back as text and not ciphertext. So independent of any telemetry bug, syncing a secret to a SaaS tool is handing that secret to the vendor. That is not an accusation. That is just what the word "sync" unpacks to.

Which is why the analytics capture, real or since-patched, is almost a distraction. It is the loud, catchable version of a quiet fact that was always true: if a vendor's software can resolve your secret, transmit it, or show it back to you, then the vendor can log it, and whether they do on any given Tuesday is a policy choice you do not control and cannot audit. The masking toggle protects your secret from the people in the room with you. It was never going to protect it from the software you handed it to.

So do not trust me on any of this, and do not trust the original post either. The good part of this whole story is that it is trivially checkable. Put mitmproxy or Charles in front of your own Postman client, mark a variable secret, fire a single request, and read the flows. You will see exactly what leaves your machine and where it goes, for your version, today, no summary in between. It is an afternoon of work and you will never look at that lock icon the same way.

## The takeaway

- The secret toggle is a privacy screen, not encryption. It hides the value from a human looking at your screen. It hides nothing from the software, which resolves the real value the instant you send the request.
- Anything you put in a cloud-synced tool is on the vendor's servers in a form they can read, because it syncs back to you as text. Assume that with or without a telemetry bug. Sync is the exfiltration you agreed to.
- Real secrets belong in a real secrets manager, and get injected at request time, not saved into the tool. Reference a secret you can rotate and revoke, do not store a copy you cannot.
- Verify instead of believing, mine included. Point a proxy you control at your own client and watch one request go out. What actually crossed the wire is the only source of truth here, and it takes an afternoon to get it.
- If you have been keeping production secrets in synced Postman environments, they have already left your machine, and you cannot know who or what read them on the way. Rotate on that assumption, not on whether you have seen proof.

Mark a variable secret, send one request through a proxy you own, and read what actually went over the wire. The dots were only ever for you.
