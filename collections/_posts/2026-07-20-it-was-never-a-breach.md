---
layout:     post
title:      It Was Never a Breach
date:       2026-07-20
description:    Someone crawled 30,000 public Postman workspaces for a year and pulled out live GitHub, Razorpay, and Okta credentials. Nothing was hacked. The tool worked exactly as designed. The real bug is that Postman is a credential store wearing a testing-tool costume, and opt-in security protects nobody who needs it.
categories: security api-keys secrets postman devsecops
---

If you have ever saved an API key into a Postman environment so you could test an endpoint without retyping the token, this post is about that key, and it is not the reassuring kind.

Here is what happened. Over about a year, researchers pointed crawlers at public Postman workspaces, roughly 30,000 of them, and pulled out live credentials by the thousand. GitHub tokens, Slack tokens, Salesforce and Microsoft logins, Razorpay and Stripe payment keys, Okta IAM credentials, New Relic keys, admin logins to support portals. Real keys, still valid, attached to real companies including a pile of Fortune 500 names. A separate scan put harder numbers on the same pattern: about 84,000 secrets across 200,000 environments, and 86 percent of them were hardcoded JSON Web Tokens, which is the sound of a whole industry pasting a live token into a variable to make one request work and never taking it back out.

The headlines called it a breach. That word is doing a lot of dishonest work. Nothing was breached. Postman was not hacked, no exploit was chained, no zero-day was burned. The tool did exactly what it was built to do, which was store the values you gave it and share the workspace you told it to share. Every one of those keys was placed there by a developer and made public by a developer. The crawler just had to show up and read.

## The problem

The problem is not Postman. The problem is that nobody thinks of Postman as the thing it actually is.

You open it to test an API. It looks like a testing tool, it behaves like a testing tool, so it lives in your head next to curl and the browser dev tools. But an environment with your tokens saved in it is not a testing tool, it is a database of live production secrets. Save fifteen of them across a few environments and you are running a small unmanaged credential vault, except you never decided to run one, so it has none of the rules a vault would have. No least privilege, no encryption you can point to, no rotation, no owner. Just strings, sitting there, because you were going to clean them up later.

Now add the one feature that turns a private mess into a public incident: the visibility toggle. A workspace can be public. People flip it for ordinary reasons, to share a collection with a teammate, to publish API docs, or they sync a collection into a public GitHub repo without stripping it first. And a public workspace has a URL, and that URL is crawlable, and search engines index it. So the chain that leaks your Razorpay key is not an attack. It is: environment variable in plaintext, workspace set to public, page indexed, key is now a search result. Four boring steps, zero of them adversarial until the very end.

People will tell you Postman has secret-masking and encrypted variables built in, so this is a user error, not a tool problem. Both things are true and it does not help. The features are opt-in. And opt-in security has a specific failure mode: the person who leaks the secret is precisely the person who did not open the settings page and turn the feature on. Security that only works if you were already careful only ever protects the people who did not need protecting. The careless case, which is the entire risk, is exactly the case the feature does not cover.

None of this is special to Postman either, and that is the part worth keeping. This is the same bug as a `.env` file committed to a public repo, a token pasted into a Slack channel, a password dropped into a Jira ticket to unblock someone. Any tool that can hold a string can hold a credential. Any tool that can share can leak one. Postman is not uniquely broken, it is just the tool that happened to get crawled at scale this year, and next year it will be a different logo on the same story.

And the thing that leaks is never just "a key," which is why the blast radius keeps surprising people. A token is a capability, and it can do whatever that capability allows, which is usually more than the person who pasted it was thinking about. A New Relic key does not just read a dashboard, it exposes system logs, usage data, and network traffic, which together draw a map of your internal infrastructure for whoever comes next. An Okta IAM credential is not one app, it is the front door to all of them. A payment key moves money. In one of these scans a single leaked AWS key led straight into a startup's CloudWatch logs, and the logs held the backend architecture and more secrets, so the one key was not the prize, it was the map to everything else. An admin login to a support portal lets someone read customer data and plant fake articles under your brand. You leaked one string. You exposed everything that string was allowed to touch.

## The takeaway

- Treat anything that stores a credential as a credential store, no matter what the icon looks like. If Postman, or a Slack channel, or a wiki page holds a live secret, it inherits the rules of a vault: least privilege, no plaintext, an owner, rotation. The tool not looking like a vault does not exempt it from being one.
- Assume opt-in protection is off. If a safeguard requires a setting, assume nobody flipped it, on every workspace, including yours. Design as if the masking is disabled, because for the leaky case it always is.
- Private by default, and treat "we will sanitize it before we share" as a wish, not a control. If a workspace has ever held a real secret, it should never have been public, and the sanitize step is exactly the step people skip.
- Rotate what has already been exposed instead of reasoning about whether anyone saw it. If a key ever sat in a public workspace, it is burned. You cannot un-index a page, and you have no idea who crawled it before you noticed. Rotate it and move on.
- The scan is cheap, and it is cheap for the attacker too. Someone can grep the entire public internet for your tokens for roughly free. You can grep your own workspaces for the same tokens in an afternoon. Only one of you is doing it right now.

Go open your team's Postman, filter to the public workspaces, and actually read what is sitting in the environments. Whatever you find in there, assume someone with a crawler found it first.
