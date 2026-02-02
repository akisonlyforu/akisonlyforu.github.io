# reCAPTCHA Setup Guide

## Overview
This guide explains how to complete the reCAPTCHA v3 integration for your contact form.

## Step 1: Get reCAPTCHA v3 Keys

1. Go to [Google reCAPTCHA Admin Console](https://www.google.com/recaptcha/admin)
2. Click "+" to create a new site
3. Configure:
   - **Label**: Your site name
   - **reCAPTCHA type**: Select "reCAPTCHA v3"
   - **Domains**: Add your domain (e.g., `akisonlyforu.github.io`)
   - Accept terms and submit

4. Copy the **Site Key** and **Secret Key**

## Step 2: Update Frontend Configuration

Replace `YOUR_SITE_KEY_HERE` in these files with your actual Site Key:

### In `contact.md`:
```html
<script src="https://www.google.com/recaptcha/api.js?render=6LdZHtcrAAAAAJmF3TOHhFZ3pXruQj1qPzXReK-N"></script>
```

### In `assets/js/contact.js`:
```javascript
const RECAPTCHA_SITE_KEY = 'YOUR_ACTUAL_SITE_KEY';
```

## Step 3: Update Google Apps Script Backend

Your Google Apps Script needs to verify the reCAPTCHA token. Add this code:

```javascript
function doGet(e) {
  const params = e.parameter;

  // Extract reCAPTCHA token
  const recaptchaResponse = params['g-recaptcha-response'];

  if (!recaptchaResponse) {
    return ContentService
      .createTextOutput('reCAPTCHA verification required')
      .setMimeType(ContentService.MimeType.TEXT);
  }

  // Verify reCAPTCHA token
  const isValidCaptcha = verifyRecaptcha(recaptchaResponse);

  if (!isValidCaptcha) {
    return ContentService
      .createTextOutput('reCAPTCHA verification failed')
      .setMimeType(ContentService.MimeType.TEXT);
  }

  // Continue with your existing form processing...
  const name = params.name;
  const email = params.email;
  const subject = params.subject;
  const message = params.message;

  // Your existing email sending logic here

  return ContentService
    .createTextOutput('Message sent successfully')
    .setMimeType(ContentService.MimeType.TEXT);
}

function verifyRecaptcha(token) {
  const SECRET_KEY = 'YOUR_SECRET_KEY_HERE'; // Replace with your actual secret key
  const url = 'https://www.google.com/recaptcha/api/siteverify';

  const payload = {
    'secret': SECRET_KEY,
    'response': token
  };

  const options = {
    'method': 'POST',
    'payload': payload
  };

  try {
    const response = UrlFetchApp.fetch(url, options);
    const result = JSON.parse(response.getContentText());

    // Check if verification was successful and score is acceptable
    // reCAPTCHA v3 returns a score from 0.0 to 1.0
    // 1.0 = very likely human, 0.0 = very likely bot
    const isSuccess = result.success === true;
    const score = result.score || 0;
    const hasValidAction = result.action === 'contact_form';

    // You can adjust the score threshold (0.5 is reasonable)
    const scoreThreshold = 0.5;

    console.log('reCAPTCHA verification:', {
      success: isSuccess,
      score: score,
      action: result.action,
      hostname: result.hostname
    });

    return isSuccess && score >= scoreThreshold && hasValidAction;

  } catch (error) {
    console.error('reCAPTCHA verification error:', error);
    return false;
  }
}
```

## Step 4: Security Configuration

### Environment Variables (Recommended)
Store your secret key as a script property instead of hardcoding:

1. In Google Apps Script editor: Project Settings → Script Properties
2. Add property: `RECAPTCHA_SECRET_KEY` = `your_secret_key`
3. Update code:
```javascript
const SECRET_KEY = PropertiesService.getScriptProperties().getProperty('RECAPTCHA_SECRET_KEY');
```

### Score Threshold
- Default: `0.5` (moderate protection)
- Strict: `0.7-0.9` (may block some legitimate users)
- Lenient: `0.1-0.3` (allows more submissions through)

## Step 5: Testing

1. Deploy your updated Google Apps Script
2. Test the contact form
3. Check Google Apps Script logs for reCAPTCHA verification results
4. Monitor the reCAPTCHA admin console for traffic analytics

## Step 6: Monitoring

- **Google Apps Script Logs**: Monitor verification attempts and failures
- **reCAPTCHA Admin Console**: View traffic patterns and bot detection analytics
- **Analytics**: Track form submission success/failure rates

## Troubleshooting

### Common Issues:
1. **"reCAPTCHA not loaded"**: Check if site key is correct and domain is registered
2. **Low scores**: Legitimate users getting blocked - lower the score threshold
3. **High bot traffic**: Increase score threshold or add additional validation

### Debug Mode:
Add console logging to see reCAPTCHA verification details:
```javascript
console.log('reCAPTCHA verification result:', result);
```

## Security Notes

- Never expose your Secret Key in frontend code
- Monitor reCAPTCHA admin console for unusual patterns
- Consider implementing rate limiting in addition to reCAPTCHA
- Regularly review and adjust score thresholds based on your traffic patterns