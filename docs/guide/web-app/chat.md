<!--docs
title = "Chat"
summary = "Keep text conversations, choose models, add attachments and retry answers."
order = 20
covers = ["/chat"]
-->
# Chat

Keep a conversation with a permitted model or compare two models side by side.

## Start a conversation {#chat}

1. Open **Text chat**, choose a model beside **Send**, and enter your message.
2. Open **Options** before sending if you need a **System prompt**, attachments or a **Comparison model**.
3. Select **Send**. Continue in the same conversation or choose **New conversation** in the sidebar.

## Options and keys {#chat-options}

| Control | Use |
|---|---|
| **Comparison model** | Start a new comparison with a second permitted model. |
| **Attach → Choose** | Add images or text files. Image input requires a model that supports it; JavaScript enables uploads. |
| **Reasoning** | Request reasoning behavior where the selected model supports it. |
| **System prompt** | Set instructions for this conversation. |
| **temperature**, **top_k**, **top_p**, **min_p** | Leave blank to use the model's own sampling values. |
| **Temporary** | Appears only when your account may use the installation's no-log mode. |
| **Plugin conversation** | Choose fresh supplied history or a persistent agent session for a plugin model. |

Enter sends on a desktop keyboard; Shift+Enter adds a line. On phones, Enter adds a line. Saved conversation options carry into follow-ups. Attachment policy is shown beside **Attach**; administrators can inspect its [generated defaults](/docs/operations/settings-reference).

## Your conversations {#chat-list}

Use **Find a conversation** in the sidebar. Double-click a title or press F2 on its heading to rename it. **Save** applies the new name. The delete button requests confirmation.

On smaller screens open **Chat options**, or **Menu** on phones, and switch between **Chat options** and **Main menu** at the top of the drawer.

## Limits and troubleshooting

**Stop** interrupts the current reply; **Ask again** retries the latest prompt. A model may need to load or its compute node may need to wake. If no models appear, ask an administrator to grant model access. Automatic conversation expiry follows site policy; it preserves active answers. Older context can be summarized while original messages remain visible.

## Related

[Your first chat](/docs/getting-started/first-chat) · [Tools and Tool chat](/docs/web-app/tools) · [API keys](/docs/web-app/api-keys)
