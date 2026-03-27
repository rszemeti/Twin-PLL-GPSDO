/*
 * Minimal PPS-Synchronized Frequency Counter Test
 * Using the exact same technique as main firmware
 * 
 * GPIO2 (pin 4): 1PPS input (rising edge)
 * GPIO3 (pin 5): 10MHz input (PWM edge counter, PWM_DIV_B_RISING mode)
 * 
 * On each 1PPS edge: read PWM counter + wrap count and calculate total count
 * Expected: ~10,000,000 counts per PPS period
 */

#include <Arduino.h>
#include <stdio.h>
#include <hardware/pwm.h>
#include <hardware/gpio.h>
#include <hardware/irq.h>

#define FREQ_PIN 3        // GPIO3, 10MHz input (PWM edge counter)
#define PPS_PIN 2         // GPIO2, 1PPS input
#define LED_PIN 25        // LED

// PWM edge counter state
static volatile uint32_t freq_wrap_count = 0;   // Wrap-around counter
static volatile uint16_t last_pwm_counter = 0;  // Last captured PWM counter value
static volatile uint32_t last_wrap_count = 0;   // Last captured wrap count
static volatile uint32_t pps_count = 0;         // Number of PPS edges seen
static volatile uint32_t counts_in_pps = 0;     // Counts between last PPS edges
static volatile bool first_window = true;       // Skip first measurement
static volatile bool measurement_ready = false;

static uint slice_num = 0;                // PWM slice for FREQ_PIN

/* PWM wrap interrupt - increment wrap counter */
void pwm_wrap_irq_handler() {
    if (pwm_get_irq_status_mask() & (1u << slice_num)) {
        pwm_clear_irq(slice_num);
        freq_wrap_count++;
    }
}

/* PPS rising edge interrupt - capture frequency count */
void gpio_pps_handler(uint gpio, uint32_t events) {
    if (gpio == PPS_PIN && (events & GPIO_IRQ_EDGE_RISE)) {
        pps_count++;
        
        // Read PWM counter and wrap count
        uint16_t current_pwm = pwm_get_counter(slice_num);
        uint32_t current_wraps = freq_wrap_count;
        
        if (first_window) {
            // Initialize baseline on first edge
            first_window = false;
            last_pwm_counter = current_pwm;
            last_wrap_count = current_wraps;
        } else {
            // Calculate counts in this PPS period
            uint32_t delta_wraps = current_wraps - last_wrap_count;
            int32_t delta_counter = (int32_t)current_pwm - (int32_t)last_pwm_counter;
            
            // Total count = wraps * 65536 + counter delta
            counts_in_pps = (delta_wraps * 65536u) + (uint32_t)(delta_counter & 0xFFFF);
            
            // Update baseline
            last_pwm_counter = current_pwm;
            last_wrap_count = current_wraps;
            measurement_ready = true;
        }
        
        // Blink LED
        digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    }
}

void setup() {
    Serial.begin(115200);
    delay(500);
    
    Serial.println("\n{\"test\": \"pps_freq_counter\"}");
    
    // Initialize LED
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);
    
    // ===== Setup GPIO3 as PWM edge counter (10MHz) =====
    gpio_set_function(FREQ_PIN, GPIO_FUNC_PWM);
    slice_num = pwm_gpio_to_slice_num(FREQ_PIN);
    
    pwm_config cfg = pwm_get_default_config();
    pwm_config_set_clkdiv_mode(&cfg, PWM_DIV_B_RISING);  // Count rising edges on B pin
    pwm_config_set_wrap(&cfg, 0xFFFF);                   // 16-bit counter
    
    pwm_init(slice_num, &cfg, false);
    pwm_set_counter(slice_num, 0);
    
    // Enable PWM wrap interrupt
    pwm_clear_irq(slice_num);
    pwm_set_irq_enabled(slice_num, true);
    irq_set_exclusive_handler(PWM_IRQ_WRAP, pwm_wrap_irq_handler);
    irq_set_enabled(PWM_IRQ_WRAP, true);
    
    pwm_set_enabled(slice_num, true);
    
    // ===== Setup GPIO2 as 1PPS input =====
    gpio_init(PPS_PIN);
    gpio_set_dir(PPS_PIN, GPIO_IN);
    gpio_pull_down(PPS_PIN);
    
    gpio_set_irq_enabled_with_callback(PPS_PIN, GPIO_IRQ_EDGE_RISE, true, &gpio_pps_handler);
    
    Serial.println("{\"event\": \"ready\"}");
}

void loop() {
    if (measurement_ready) {
        noInterrupts();
        const uint32_t sample_pps = pps_count;
        const uint32_t sample_counts = counts_in_pps;
        measurement_ready = false;
        interrupts();

        printf("{\"pps\": %lu, \"counts\": %lu}\n", sample_pps, sample_counts);
    }

    delay(100);
}
