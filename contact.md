---
layout: page
permalink: /contact/
---

<div class="contact-container">
  <h1>Get in Touch</h1>
  <p class="contact-intro">Have a question or want to discuss something? Feel free to reach out using the form below.</p>

  <form action="https://script.google.com/macros/s/AKfycbxg5vk4drzb36TM3dsZsAkzpRfr7BbdBOYQRy9ffkBXhXRxNxuxNVImte94ijvmQE-vsw/exec" method="GET" class="contact-form">
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
  color: var(--text-secondary);
  margin-bottom: 2rem;
}

.contact-form {
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
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
  color: var(--text-primary);
  font-weight: 500;
}

.form-group input,
.form-group textarea {
  width: 100%;
  padding: 0.75rem;
  border: 1px solid var(--input-border);
  background-color: var(--input-bg);
  color: var(--input-text);
  border-radius: 4px;
  font-size: 1rem;
  transition: border-color 0.3s ease;
}

.form-group input:focus,
.form-group textarea:focus {
  outline: none;
  border-color: var(--input-border-focus);
}

.submit-button {
  background-color: var(--button-bg);
  color: var(--button-text);
  padding: 0.75rem 1.5rem;
  border: none;
  border-radius: 4px;
  font-size: 1rem;
  cursor: pointer;
  transition: all 0.3s ease;
  position: relative;
}

.submit-button:hover {
  background-color: var(--button-bg-hover);
  transform: translateY(-1px);
}

.submit-button:active {
  transform: translateY(0);
}

.submit-button:disabled {
  cursor: not-allowed;
  background-color: var(--text-tertiary);
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
  background-color: var(--accent-color, #28a745);
  color: #ffffff;
  font-weight: bold;
  text-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
}

#form-message.error {
  background-color: #dc3545;
  color: #ffffff;
}
</style>

<script src="/assets/js/contact.js"></script>