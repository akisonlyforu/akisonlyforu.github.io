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
      
      const name = form.name.value;
      const email = form.email.value;
      const subject = form.subject.value;
      const message = form.message.value;
      
      const params = new URLSearchParams({name: name, email: email, subject: subject, message: message});

      await fetch(url, {
        method: 'GET',
        redirect: "manual",
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'Accept': 'application/json'
        }
      }).then(response => {
        console.log("Status:", response.status);  // Should show 302
        console.log("Location Header:", response.headers.get("location")); 
      });
      
      // Since mode is 'no-cors', we can't read the response
      // Just assume success if no error was thrown
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