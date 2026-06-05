# SIM Number Editing Feature Documentation

## Overview
Added the ability to edit the SIM card number (SIM 卡号) in the /modem page with a light-colored pencil icon, allowing users to customize their displayed phone number and save it to the server.

## Features Implemented

### 1. Frontend UI Enhancement
**Location**: `app/templates/modem.html` lines 430-470

#### Visual Design
- **Light pencil icon**: Subtle gray edit icon with hover effects
- **Inline editing**: Click to edit directly in the table
- **Input validation**: Real-time validation with save/cancel buttons
- **Smooth transitions**: Hover effects and focus states for better UX

#### User Interface Elements
- **Display mode**: Shows current SIM number with light pencil icon
- **Edit mode**: 
  - Text input field (32 characters width)
  - Green checkmark (save) button
  - Red X (cancel) button
  - Enter key to save, Escape key to cancel

#### Styling Features
- Light gray pencil icon (`text-gray-400`) that becomes darker on hover
- Clean, minimalist design integrated with existing table style
- Responsive design for mobile devices
- Visual feedback for all interactions

### 2. JavaScript Functionality
**Location**: `app/templates/modem.html` lines 2229-2291

#### Data Management
```javascript
editing_sim_number: false,    // Track edit state
temp_sim_number: "",         // Temporary storage during editing
```

#### Core Functions
- **`startEditSimNumber()`**: 
  - Enters edit mode
  - Pre-fills input with current value
  - Auto-focuses and selects text
  
- **`cancelEditSimNumber()`**: 
  - Exits edit mode without saving
  - Resets temporary value

- **`saveSimNumber()`**: 
  - Validates input format
  - Sends API request to server
  - Updates local display on success
  - Shows success/error messages

#### Input Validation
- **Empty check**: Ensures number is not blank
- **Format validation**: Regex pattern `^\+?[0-9\s\-\(\)]{10,20}$`
- **Length check**: 10-20 characters including formatting
- **International support**: Accepts + prefix for country codes

### 3. Backend API Endpoint
**Location**: `app/pcat_manager_web.py` lines 2994-3043

#### Endpoint Details
- **URL**: `POST /api/v1/modem/sim_number.json`
- **Authentication**: Requires API authentication (`@api_auth_required`)
- **Content-Type**: `application/json`

#### Request Format
```json
{
  "sim_number": "+1234567890"
}
```

#### Response Format
**Success:**
```json
{
  "status": "ok",
  "message": "SIM number updated successfully", 
  "sim_number": "+1234567890"
}
```

**Error:**
```json
{
  "status": "error",
  "message": "Invalid phone number format"
}
```

#### Server-Side Validation
- **Input sanitization**: Strips whitespace
- **Empty validation**: Checks for non-empty input
- **Regex validation**: Server-side phone number format check
- **Thread safety**: Uses modem client mutex for concurrent access

### 4. Data Persistence
**Location**: `app/pcat_manager_web.py` lines 3021, 2881-2882

#### Storage Mechanism
- **Configuration storage**: `pcat_config.set_user_config_key("custom_sim_number", sim_number)`
- **Runtime update**: Updates `pc_app.modem_client.sim_number` for immediate display
- **Priority system**: Custom number overrides auto-detected number

#### Data Retrieval
- **Priority**: Custom user-set number takes precedence
- **Fallback**: Uses auto-detected SIM number if no custom number set
- **Thread-safe**: Protected by modem client mutex

```python
# Priority system in API response
custom_sim_number = pcat_config.get_user_config_key("custom_sim_number", None)
response_json["sim_number"] = custom_sim_number if custom_sim_number else pc_app.modem_client.basic.get("sim_number", "")
```

## User Experience Flow

### 1. View Mode
- User sees SIM number with a subtle light gray pencil icon
- Icon appears on hover with tooltip "Edit SIM Number"

### 2. Edit Mode
- Click pencil icon to enter edit mode
- Input field appears with current number pre-selected
- Save (✓) and Cancel (✕) buttons appear
- Auto-focus and text selection for easy editing

### 3. Validation & Saving
- Real-time validation prevents invalid characters
- Enter key saves, Escape key cancels
- Server validates and stores the number
- Success message confirms the update
- Page refreshes to show updated number

### 4. Error Handling
- Client-side validation for immediate feedback
- Server-side validation for security
- Clear error messages for user guidance
- Graceful fallback to original value on error

## Technical Implementation Details

### Input Validation Pattern
```javascript
// Client-side validation
const phoneRegex = /^\+?[0-9\s\-\(\)]{10,20}$/;

// Server-side validation (Python)
phone_regex = r'^\+?[0-9\s\-\(\)]{10,20}$'
```

### Configuration Persistence
```python
# Save custom number
pcat_config.set_user_config_key("custom_sim_number", sim_number)

# Load custom number on startup
custom_sim_number = pcat_config.get_user_config_key("custom_sim_number", None)
```

### Thread Safety
```python
# Protected modem client access
with pc_app.modem_client.mutex:
    pc_app.modem_client.sim_number = sim_number
```

## Security Considerations

### Input Validation
- **Sanitization**: All input is trimmed and validated
- **Length limits**: 10-20 character limit prevents abuse
- **Pattern matching**: Strict regex prevents injection attacks
- **Server-side validation**: Never trust client-side validation alone

### Authentication
- **API protection**: Requires user authentication
- **Session validation**: Uses existing authentication system
- **Logging**: All changes are logged with user IP

### Data Storage
- **Configuration file**: Stored in secure user config
- **No sensitive data**: Only stores display phone number
- **Backup**: Falls back to auto-detected number if needed

## Browser Compatibility

### Modern Features Used
- **async/await**: Modern JavaScript promise handling
- **fetch API**: Modern HTTP request handling
- **CSS Grid/Flexbox**: Modern layout systems
- **AlpineJS**: Lightweight reactive framework

### Supported Browsers
- Chrome 60+
- Firefox 55+
- Safari 11+
- Edge 79+

## Testing Scenarios

### Valid Inputs
- `+1234567890` (US number with country code)
- `(555) 123-4567` (US formatted number)
- `+86 138 0013 8000` (Chinese number with spaces)
- `020-1234-5678` (Local number with dashes)

### Invalid Inputs
- `abc123` (contains letters)
- `+1` (too short)
- `++1234567890` (invalid format)
- `+123456789012345678901` (too long)

### Error Conditions
- Empty input
- Network connection failure
- Server authentication failure
- Invalid session

## Future Enhancements

### Potential Improvements
1. **Contact Integration**: Link to contact management system
2. **International Formatting**: Auto-format based on country
3. **Bulk Import**: Import numbers from CSV/contacts
4. **History Tracking**: Keep track of number changes
5. **Validation Service**: Real-time number validation via telecom API
6. **Multiple Numbers**: Support for multiple SIM cards
7. **Auto-Detection Override**: Manual vs automatic detection settings

### UI/UX Enhancements
1. **Inline validation**: Real-time format checking as user types
2. **Format suggestions**: Show format examples based on country
3. **Undo functionality**: Ability to revert to previous number
4. **Keyboard shortcuts**: Hotkeys for edit/save/cancel
5. **Mobile optimization**: Better touch interface for mobile devices

## Troubleshooting

### Common Issues
1. **Icon not visible**: Check CSS loading and browser compatibility
2. **Save not working**: Verify API endpoint and authentication
3. **Number not persisting**: Check config file permissions
4. **Format validation failing**: Verify regex pattern matches expected format

### Debug Steps
1. **Check browser console**: Look for JavaScript errors
2. **Check server logs**: Verify API requests are reaching server
3. **Test API directly**: Use curl to test the endpoint
4. **Check config file**: Verify user config is being saved

### Log Locations
- **Server logs**: `/tmp/pcat-manager-web.log`
- **Browser console**: Developer tools console
- **Network requests**: Developer tools network tab

## Summary
The SIM number editing feature provides a polished, user-friendly way to customize the displayed phone number in the modem interface. It follows modern UI/UX patterns with inline editing, proper validation, and seamless integration with the existing interface. The feature is secure, performant, and ready for production use on Phonicat router devices.