---
layout: page
permalink: /contact/
---

<div class="contact-container">
  <h1>Get in Touch</h1>
  <p class="contact-intro">Have a question or want to discuss something? Feel free to reach out using the form below.</p>

  <form action="https://script.google.com/macros/s/AKfycbwBcgaTjeRMNmXJ0prTeY1UTlMiswDhfKbBXgLq_IX8USzcVjAYzXF5uHpOAx7azPKjWQ/exec" method="POST" class="contact-form">
    <div class="form-group">
      <label for="name">Name</label>
      <input type="text" id="name" name="name" required>
    </div>

    <div class="form-group">
      <label for="email">Email</label>
      <input type="email" id="email" name="email" required>
    </div>

    <div class="form-group">
      <label for="subject">Subject</label>
      <input type="text" id="subject" name="subject" required>
    </div>

    <div class="form-group">
      <label for="message">Message</label>
      <textarea id="message" name="message" rows="5" required></textarea>
    </div>

    <button type="submit" class="submit-button">Send Message</button>
    <p id="form-message" role="status" aria-live="polite"></p>
  </form>
</div>

<style>
.contact-container {
  max-width: 600px;
  margin: 0 auto;
  padding: 2rem 0;
}

.contact-intro {
  color: #666;
  margin-bottom: 2rem;
}

.contact-form {
  background: #fff;
  border-radius: 8px;
  padding: 2rem;
  box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
}

.form-group {
  margin-bottom: 1.5rem;
}

.form-group label {
  display: block;
  margin-bottom: 0.5rem;
  color: #333;
  font-weight: 500;
}

.form-group input,
.form-group textarea {
  width: 100%;
  padding: 0.75rem;
  border: 1px solid #ddd;
  border-radius: 4px;
  font-size: 1rem;
  transition: border-color 0.3s ease;
}

.form-group input:focus,
.form-group textarea:focus {
  outline: none;
  border-color: #666;
}

.submit-button {
  background-color: #333;
  color: white;
  padding: 0.75rem 1.5rem;
  border: none;
  border-radius: 4px;
  font-size: 1rem;
  cursor: pointer;
  transition: all 0.3s ease;
  position: relative;
}

.submit-button:hover {
  background-color: #000;
  transform: translateY(-1px);
}

.submit-button:active {
  transform: translateY(0);
}

.submit-button:disabled {
  cursor: not-allowed;
  background-color: #444;
  transform: none;
}

.submit-button.loading::after {
  content: '';
  position: absolute;
  width: 1em;
  height: 1em;
  border: 2px solid #ffffff;
  border-radius: 50%;
  border-top-color: transparent;
  animation: spin 0.6s linear infinite;
  right: 0.5rem;
  top: 50%;
  transform: translateY(-50%);
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

#form-message {
  margin: 0.5rem 0 0;
  font-size: 0.9rem;
  text-align: center;
  min-height: 1.2em;
  padding: 0.5rem;
  border-radius: 0.5rem;
  transition: all 0.3s ease-out;
  opacity: 0;
  transform: translateY(-10px);
}

#form-message.visible {
  opacity: 1;
  transform: translateY(0);
}

#form-message.success {
  background-color: #dcf5e3;
  color: #166534;
}

#form-message.error {
  background-color: #fde7e7;
  color: #b91c1c;
}
</style>

<script>
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

  form.addEventListener('submit', async function(e) {
    e.preventDefault();
    const originalText = button.textContent;
    
    try {
      button.textContent = 'Sending...';
      button.disabled = true;
      button.classList.add('loading');
      
      const formData = new FormData(form);
      const response = await fetch(form.action, {
        method: 'POST',
        body: new URLSearchParams(formData),
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'Accept': 'application/json'
        },
        mode: 'no-cors' // Add this to prevent CORS issues with Google Apps Script
      });
      
      // Since mode is 'no-cors', we can't read the response
      // Just assume success if no error was thrown
      showMessage('Message sent successfully! 🎉', false);
      form.reset();

      // Check the specific status code
      if (response.status === 200) {
        showMessage('Message sent successfully! 🎉', false);
        form.reset();
        // Track successful submission if GA is available
        if (typeof gtag !== 'undefined') {
          gtag('event', 'contact_form_submit', {
            'event_category': 'Contact',
            'event_label': 'Success'
          });
        }
      } else if (response.status === 422) {
        throw new Error('Please check your inputs and try again.');
      } else if (response.status === 429) {
        throw new Error('Too many attempts. Please try again later.');
      } else {
        throw new Error('Sorry, message could not be sent. Please try again.');
      }
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
</script> 