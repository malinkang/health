#  Your Health Data

This repo is automatically updated by Hadge. If you want to modify the files, create a new branch first. Files in the main branch will be automatically overwritten.

## File Formats

### Activity

The folder `activity` contains activity data from the Activity app. One csv file per year. If you don't use the Apple Watch. there will be no meaningful data in it.

| Column | Description |
| --------- | ------------- |
| Date | The date formatted as yyyy-MM-dd |
| Move Actual | Energy burned in kcal |
| Move Goal | Your personal move goal in kcal |
| Exercise Actual | Number of exercise minutes |
| Exercise Goal | Your personal exercise goal (this cannot be set in iOS, it's always 30min) |
| Stand Actual | Number of stand hours |
| Stand Goal |Your personal stand goal (this cannot be set in iOS, it's always 12h) |

### Distances

The folder `distances` contains distance, walking and running steps, and swimming strokes data. One csv file per year.  

| Column | Description |
| --------- | ------------- |
| Date | The date formatted as yyyy-MM-dd |
| Distance Walking/Running | In meters |
| Steps | Step count for the given date |
| Distance Swimming | In meters |
| Strokes | Stroke count for all swimming workouts on this date |
| Distance Cycling | In meters |
| Distance Wheelchair | In meters |
| Distance Downhill Snowsports | In meters |

### Workouts

The folder `workouts` contains the data for all your workouts. One csv file per year.  

| Column | Description |
| --------- | ------------- |
| UUID | A unique identifier |
| Start Date | Start date/time of the workout, formatted as ISO 8601 (yyyy-MM-dd'T'HH:mm:ssZ) |
| End Date | End date/time of the workout, formatted as ISO 8601 (yyyy-MM-dd'T'HH:mm:ssZ) |
| Type | Workout type as an integer, for example 52 |
| Name | Workout type as string, for example Walking |
| Duration | In seconds |
| Distance | In meters |
| Elevation Ascended | In meters |
| Flights Climbed | Number of flights taken during the workout |
| Swim Strokes | Stroke count for swimming workouts |
| Total Energy | In kcal |

### Additional Health Data

Hadge can export eight optional HealthKit modules. Each module is stored in its own folder with one CSV file per year. Modules can be enabled or disabled in **Settings → Sync**. Disabling a module stops future uploads and does not delete existing files.

| Folder | Data | Columns |
| --- | --- | --- |
| `body` | Weight, BMI, body fat, lean mass, height, waist circumference | UUID, Start Date, End Date, Type, Value, Unit, Source |
| `heart-rate` | Daily minimum, maximum, and average heart rate | Date, Minimum, Maximum, Average, Unit |
| `vitals` | Resting/walking heart rate, HRV, respiratory rate, blood oxygen, VO2 Max, temperature, blood glucose | UUID, Start Date, End Date, Type, Value, Unit, Source |
| `sleep` | In-bed, awake, core, deep, REM, and unspecified sleep samples | UUID, Start Date, End Date, Type, Value, Source |
| `blood-pressure` | Correlated systolic and diastolic readings | UUID, Start Date, End Date, Systolic, Diastolic, Unit, Source |
| `nutrition` | Water, calories, protein, carbohydrates, fat, fiber, sugar, sodium, caffeine | UUID, Start Date, End Date, Type, Value, Unit, Source |
| `mobility` | Walking speed, step length, asymmetry, double support, stair speed, walking steadiness | UUID, Start Date, End Date, Type, Value, Unit, Source |
| `mindfulness` | Mindful sessions | UUID, Start Date, End Date, Type, Value, Source |

Only samples available on the device and authorized by the user are exported. Hadge does not export clinical records, reproductive health, medications, symptoms, or workout GPS routes as part of these modules.
