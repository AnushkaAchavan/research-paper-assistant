/* Safe, non-destructive SQL Server schema for PaperMind. */
IF OBJECT_ID(N'dbo.Users', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Users (
        user_id INT IDENTITY(1,1) NOT NULL,
        name NVARCHAR(100) NOT NULL,
        email NVARCHAR(255) NOT NULL,
        password_hash NVARCHAR(255) NOT NULL,
        created_at DATETIME2(0) NOT NULL CONSTRAINT DF_Users_created_at DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_Users PRIMARY KEY CLUSTERED (user_id),
        CONSTRAINT UQ_Users_email UNIQUE (email)
    );
END;
GO

IF COL_LENGTH(N'dbo.Users', N'created_at') IS NULL
    ALTER TABLE dbo.Users ADD created_at DATETIME2(0) NULL;
GO

IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID(N'dbo.Users')
      AND name = N'created_at'
      AND is_nullable = 1
)
BEGIN
    UPDATE dbo.Users SET created_at = SYSUTCDATETIME() WHERE created_at IS NULL;
    ALTER TABLE dbo.Users ALTER COLUMN created_at DATETIME2(0) NOT NULL;
END;
GO

IF OBJECT_ID(N'dbo.Papers', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Papers (
        paper_id INT IDENTITY(1,1) NOT NULL,
        filename NVARCHAR(512) NOT NULL,
        title NVARCHAR(512) NOT NULL,
        upload_date DATETIME2(0) NOT NULL CONSTRAINT DF_Papers_upload_date DEFAULT SYSUTCDATETIME(),
        user_id INT NULL,
        CONSTRAINT PK_Papers PRIMARY KEY CLUSTERED (paper_id),
        CONSTRAINT CK_Papers_filename_not_empty CHECK (LEN(LTRIM(RTRIM(filename))) > 0),
        CONSTRAINT FK_Papers_Users FOREIGN KEY (user_id) REFERENCES dbo.Users (user_id)
    );
END;
GO

/* Existing installs retain legacy papers unassigned instead of guessing an owner. */
IF COL_LENGTH(N'dbo.Papers', N'user_id') IS NULL
    ALTER TABLE dbo.Papers ADD user_id INT NULL;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.foreign_keys
    WHERE name = N'FK_Papers_Users'
      AND parent_object_id = OBJECT_ID(N'dbo.Papers')
)
    ALTER TABLE dbo.Papers ADD CONSTRAINT FK_Papers_Users
        FOREIGN KEY (user_id) REFERENCES dbo.Users (user_id);
GO

IF OBJECT_ID(N'dbo.Chunks', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Chunks (
        chunk_id INT IDENTITY(1,1) NOT NULL,
        paper_id INT NOT NULL,
        chunk_text NVARCHAR(MAX) NOT NULL,
        page INT NOT NULL,
        section NVARCHAR(512) NOT NULL,
        CONSTRAINT PK_Chunks PRIMARY KEY CLUSTERED (chunk_id),
        CONSTRAINT FK_Chunks_Papers FOREIGN KEY (paper_id) REFERENCES dbo.Papers (paper_id),
        CONSTRAINT CK_Chunks_page_positive CHECK (page > 0),
        CONSTRAINT CK_Chunks_text_not_empty CHECK (LEN(LTRIM(RTRIM(chunk_text))) > 0)
    );
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_Chunks_paper_id_chunk_id' AND object_id = OBJECT_ID(N'dbo.Chunks'))
    CREATE INDEX IX_Chunks_paper_id_chunk_id ON dbo.Chunks (paper_id, chunk_id);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_Papers_user_id_upload_date' AND object_id = OBJECT_ID(N'dbo.Papers'))
    CREATE INDEX IX_Papers_user_id_upload_date ON dbo.Papers (user_id, upload_date DESC);
GO
