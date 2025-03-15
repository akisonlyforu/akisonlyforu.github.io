document.addEventListener('DOMContentLoaded', function() {
  const form = document.getElementById('subscribe-form');
  if (!form) return;

  form.addEventListener('submit', async function(e) {
    e.preventDefault(); // Prevent form from submitting normally
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
        },
        mode: 'cors', // Enable CORS
        redirect: 'manual' // Prevent automatic redirects
      });
      
      // Check the specific status code
      if (response.status === 200) {
        showMessage('Thank you for subscribing!', false);
        form.reset();
      } else if (response.status === 422) {
        throw new Error('Please enter a valid email address.');
      } else if (response.status === 429) {
        throw new Error('Too many attempts. Please try again later.');
      } else {
        throw new Error('Sorry, submission failed. Status: ' + response.status);
      }
    } catch (error) {
      showMessage(error.message, true);
      console.error('Subscription error:', error);
    } finally {
      button.value = originalButtonText;
      button.disabled = false;
    }
  });
}); 