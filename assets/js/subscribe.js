document.addEventListener('DOMContentLoaded', function() {
  const form = document.getElementById('subscribe-form');
  if (!form) return;

  form.addEventListener('submit', async function(e) {
    e.preventDefault();
    const messageEl = document.getElementById('subscribe-message');
    const button = form.querySelector('input[type="submit"]');
    const originalButtonText = button.value;
    
    // Show message with fade in
    const showMessage = (message, isError) => {
      messageEl.textContent = message;
      messageEl.className = isError ? 'error' : 'success';
      messageEl.style.display = 'block';
      messageEl.style.opacity = '1';
      
      // Fade out after 3 seconds
      setTimeout(() => {
        messageEl.style.opacity = '0';
        setTimeout(() => {
          messageEl.style.display = 'none';
          messageEl.textContent = '';
        }, 500); // Wait for fade out animation to complete
      }, 3000);
    };
    
    try {
      button.value = 'Subscribing...';
      button.disabled = true;
      
      const formData = new FormData(form);
      const response = await fetch(form.action, {
        method: 'POST',
        body: formData,
        headers: {
          'Accept': 'application/json',
          'X-Requested-With': 'XMLHttpRequest'
        }
      });
      
      if (response.ok) {
        showMessage('Thank you for subscribing!', false);
        form.reset();
      } else {
        throw new Error('Sorry, submission failed.');
      }
    } catch (error) {
      showMessage(error.message, true);
    } finally {
      button.value = originalButtonText;
      button.disabled = false;
    }
  });
}); 