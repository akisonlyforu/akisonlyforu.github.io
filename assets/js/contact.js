const RECAPTCHA_SITE_KEY = '6LdZHtcrAAAAAAJmF3TOHhFZ3pXruQjIqPzXReK-N';

document.addEventListener('DOMContentLoaded', function() {
  const form = document.querySelector('.contact-form');
  if (!form) return;

  const messageEl = document.getElementById('form-message');
  const button = form.querySelector('button');

  // Show message with fade in
  const showMessage = (message, isError) => {
    messageEl.textContent = message;
    messageEl.className = `${isError ? 'error' : 'success'} visible`;

    // Fade out after 3 seconds
    setTimeout(() => {
      messageEl.classList.remove('visible');
      setTimeout(() => {
        messageEl.textContent = '';
      }, 300);
    }, 3000);
  };

  // Adblockers (or a stalled ready() callback) can strip/hang the reCAPTCHA
  // script; submission must still work without a token, and must not hang.
  async function getRecaptchaToken() {
    if (typeof grecaptcha === 'undefined') return null;
    const tokenPromise = new Promise((resolve, reject) => {
      grecaptcha.ready(() => {
        grecaptcha.execute(RECAPTCHA_SITE_KEY, { action: 'contact' }).then(resolve, reject);
      });
    });
    const timeout = new Promise(resolve => setTimeout(() => resolve(null), 4000));
    try {
      return await Promise.race([tokenPromise, timeout]);
    } catch (error) {
      console.error('reCAPTCHA error:', error);
      return null;
    }
  }

  form.addEventListener('submit', async function(e) {
    e.preventDefault();
    const originalText = button.textContent;

    try {
      button.textContent = 'Sending...';
      button.disabled = true;
      button.classList.add('loading');

      const name = form.name.value;
      const email = form.email.value;
      const subject = form.subject.value;
      const message = form.message.value;

      const params = new URLSearchParams({name: name, email: email, subject: subject, message: message});
      const token = await getRecaptchaToken();
      if (token) params.append('g-recaptcha-response', token);

      // mode: 'no-cors' keeps the response opaque; we can't read status from a
      // cross-origin Apps Script endpoint, so success is assumed if fetch didn't throw.
      await fetch(form.action, {
        method: 'POST',
        body: params,
        mode: 'no-cors'
      });

      showMessage('Message sent successfully! 🎉', false);
      form.reset();
    } catch (error) {
      showMessage(error.message, true);
      console.error('Submission error:', error);
      // Track error if GA is available
      if (typeof gtag !== 'undefined') {
        gtag('event', 'contact_form_error', {
          'event_category': 'Contact',
          'event_label': error.message
        });
      }
    } finally {
      button.textContent = originalText;
      button.disabled = false;
      button.classList.remove('loading');
    }
  });
});