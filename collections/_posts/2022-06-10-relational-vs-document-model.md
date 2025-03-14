---
layout:     post  
title:      Relational Model Versus Document Model 
date:       2022-06-10  
summary:    A comparative exploration of the relational and document data models, with code samples and examples to illustrate their differences.
categories: data-models databases ddia
---

In the ever-expanding field of data management, the decision to use either the **Relational Model** or the **Document Model** can have a profound impact on system design and efficiency. This post explores their contrasts, supplemented with examples and diagrams to make the differences more tangible.
   
---  

### **Schema Flexibility**

In document databases, there’s no need for predefined schemas, allowing you to dynamically adjust the data structure. This is particularly useful in applications where frequent schema updates are necessary.

#### **Document Model Example: JSON Flexibility**
```json  
{  
    "id": 1,  
    "fullName": "Jane Doe",  
    "contact": {  
        "email": "jane.doe@example.com",  
        "phone": "123-456-7890"  
    },  
    "preferences": ["email", "sms"]  
}  
```  

Now, if you decide to add an additional field, such as `address`, you can include it directly without changing any predefined schema:
```json  
{  
    "id": 1,  
    "fullName": "Jane Doe",  
    "contact": {  
        "email": "jane.doe@example.com",  
        "phone": "123-456-7890"  
    },  
    "preferences": ["email", "sms"],  
    "address": "123 Main Street"  
}  
```  

#### **Relational Model Example: Schema-On-Write Rigor**

In relational databases, the schema must be predefined:
```sql  
CREATE TABLE Users (  
    id INT PRIMARY KEY,  
    fullName VARCHAR(100),  
    email VARCHAR(100),  
    phone VARCHAR(15)  
);  
```  

If you want to add the `address` field, you must first alter the table schema:
```sql  
ALTER TABLE Users  
ADD COLUMN address VARCHAR(255);  
```  
   
---  

### **Performance and Locality**

In document models, related data often resides in the same document for fast access.

#### **Document Model Example**
Imagine retrieving a user's profile and preferences in one shot:

```json  
{  
    "id": 1,  
    "fullName": "John Smith",  
    "education": [  
        { "degree": "B.Sc.", "year": 2010 },  
        { "degree": "M.Sc.", "year": 2015 }  
    ],  
    "jobs": [  
        { "title": "Software Engineer", "company": "ABC Inc.", "years": 3 },  
        { "title": "Senior Developer", "company": "XYZ Ltd.", "years": 5 }  
    ]  
}  
```  

#### **Relational Model Example**

Here, you must query at least two tables (*Users* and *Jobs*) and join them:

```sql  
SELECT U.fullName, J.title, J.company  
FROM Users U  
JOIN Jobs J ON U.id = J.userId  
WHERE U.id = 1;  
```  

The results would require combining multiple sets of data:
```  
+----------------+------------------+------------+  
| fullName       | title            | company    |  
+----------------+------------------+------------+  
| John Smith     | Software Engineer| ABC Inc.   |  
| John Smith     | Senior Developer | XYZ Ltd.   |  
+----------------+------------------+------------+  
```  
   
---  

### **Handling Data Relationships**

Relational databases excel at normalizing datasets into smaller, related tables.

#### **Example Relational Data: Jobs and Users**
```  
Users Table  
+----+-----------+  
| id | fullName  |  
+----+-----------+  
| 1  | John Smith|  
+----+-----------+  
   
Jobs Table  
+----+-----------+------------+  
| id | title     | userId     |  
+----+-----------+------------+  
| 1  | Engineer  | 1          |  
| 2  | Developer | 1          |  
+----+-----------+------------+  
```  

#### **Example in Document Model (Denormalized)**

In document databases, relationship data is often embedded to reduce joins:

```json  
{  
    "id": 1,  
    "fullName": "John Smith",  
    "jobs": [  
        { "title": "Engineer" },  
        { "title": "Developer" }  
    ]  
}  
```  

However, denormalization may increase redundancy and complexities if jobs are related to multiple users.
   
---  

### **Image Illustrations**

#### Relational Model (Normalized)
```plaintext  
+-----------+      +-----------+  
|  Users    |      |   Jobs    |  
+-----------+      +-----------+  
| id        |<-+   | userId    |  
| fullName  |  +-> | title     |  
+-----------+      +-----------+  
```  

#### Document Model (Denormalized)
```plaintext  
+-------------------------------------------+  
|                 User                      |  
+-------------------------------------------+  
| id          | fullName                   |  
| jobs        | [                          |  
|             |   { title: "Engineer" },   |  
|             |   { title: "Developer" }   |  
|             | ]                          |  
+-------------------------------------------+  
```  
   
---  

### **Use Case Suitability**

- **Document Model:** Ideal for loosely coupled, hierarchical data such as content management systems, catalogs, or social media profiles.
- **Relational Model:** Preferred for highly interconnected data structures like financial systems or inventory management where consistency and normalization are critical.

---  

### **Convergence of Models**

The lines are blurring, with relational databases supporting semi-structured data (e.g., PostgreSQL’s JSONB) and document databases offering query functionalities akin to joins (e.g., MongoDB's `$lookup`).

#### Relational Support for JSON (PostgreSQL Example):
```sql  
SELECT *  
FROM Users  
WHERE data->'preferences' @> '"sms"';  
```  

#### Document Model Joins (MongoDB Aggregation Example):
```javascript  
db.users.aggregate([  
    {  
        $lookup: {  
            from: "jobs",  
            localField: "id",  
            foreignField: "userId",  
            as: "associatedJobs"  
        }  
    }  
]);  
```  
   
---  

### **Final Thoughts**

The **Relational Model** and **Document Model** cater to different use cases but increasingly overlap in their capabilities. Developers can now choose based not only on current needs but also taking into account hybrid possibilities. By leveraging their evolving features, modern solutions can strike a balance between flexibility, performance, and relational robustness.